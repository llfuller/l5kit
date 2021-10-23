import os
import pathlib
import random
import subprocess
from pathlib import Path

os.environ['OMP_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

import numpy as np
import torch
from l5kit.configs import load_config_data
from l5kit.data import ChunkedDataset, LocalDataManager
from l5kit.dataset import EgoDataset
from l5kit.environment.utils import get_scene_types, get_scene_types_as_dict
from l5kit.kinematic import AckermanPerturbation
from l5kit.planning.rasterized.model import RasterizedPlanningModel
from l5kit.random import GaussianRandomGenerator
from l5kit.rasterization import build_rasterizer
from stable_baselines3.common import utils
from torch import nn, optim
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from tqdm import tqdm

from drivenet_eval import eval_model
from dro_utils import append_group_index, append_reward_scaling, get_sample_weights, subset_and_subsample
from group_dro_loss import LossComputer
from vrex_loss import VRexLossComputer

# Dataset is assumed to be on the folder specified
# in the L5KIT_DATA_FOLDER environment variable
# Please set the L5KIT_DATA_FOLDER environment variable
DEFAULT_L5KIT_DATA_FOLDER = '/tmp/datasets/l5kit_data'
if "L5KIT_DATA_FOLDER" not in os.environ:
    os.environ["L5KIT_DATA_FOLDER"] = DEFAULT_L5KIT_DATA_FOLDER
    if not os.path.exists(DEFAULT_L5KIT_DATA_FOLDER):
        # Download data
        subprocess.call(str( Path(__file__).parents[1] / 'download_data.sh'))

path_l5kit = Path(__file__).parents[2]
path_examples = Path(__file__).parents[1]
path_dro = Path(__file__).parent

scene_id_to_type_path = str(path_l5kit / "dataset_metadata/validate_turns_metadata.csv")

dm = LocalDataManager(None)
# get config
cfg = load_config_data(str(path_dro / "drivenet_config.yaml"))

# Logging and Saving
output_name = cfg["train_params"]["output_name"]
if cfg["train_params"]["save_relative"]:
    save_path = path_dro / "checkpoints"
else:
    save_path = "/opt/ml/checkpoints/checkpoints/"
save_path.mkdir(parents=True, exist_ok=True)
if cfg["train_params"]["log_relative"]:
    logger = utils.configure_logger(0, str(path_dro / "drivenet_logs"), output_name, True)
else:
    logger = utils.configure_logger(0, "/opt/ml/checkpoints/drivenet_logs", output_name, True)

seed = cfg['train_params']['seed']
torch.manual_seed(seed)
random.seed(seed)
np.random.seed(seed)

# rasterisation and perturbation
rasterizer = build_rasterizer(cfg, dm)
mean = np.array([0.0, 0.0, 0.0])  # lateral, longitudinal and angular
std = np.array([0.5, 1.5, np.pi / 6])
perturb_prob = cfg["train_data_loader"]["perturb_probability"]
perturbation = AckermanPerturbation(
    random_offset_generator=GaussianRandomGenerator(mean=mean, std=std), perturb_prob=perturb_prob)

# Train Dataset
train_zarr = ChunkedDataset(dm.require(cfg["train_data_loader"]["key"])).open()
train_dataset_original = EgoDataset(cfg, train_zarr, rasterizer, perturbation)
cumulative_sizes = train_dataset_original.cumulative_sizes

# if "SCENE_ID_TO_TYPE" not in os.environ:
#     raise KeyError("SCENE_ID_TO_TYPE environment variable not set")
# scene_id_to_type_mapping_file = os.environ["SCENE_ID_TO_TYPE"]
scene_id_to_type_mapping_file = str(path_l5kit / "dataset_metadata/train_turns_metadata.csv")
scene_type_to_id_dict = get_scene_types_as_dict(scene_id_to_type_mapping_file)
scene_id_to_type_list = get_scene_types(scene_id_to_type_mapping_file)
num_groups = len(scene_type_to_id_dict)
group_counts = torch.IntTensor([len(v) for k, v in scene_type_to_id_dict.items()])
group_str = [k for k in scene_type_to_id_dict.keys()]
reward_scale = {"straight": 1.0, "left": 19.5, "right": 16.6}

# Train evaluation Dataset
train_eval_cfg = cfg["train_data_loader"]
train_eval_zarr = ChunkedDataset(dm.require(train_eval_cfg["key"])).open()
train_eval_dataset = EgoDataset(cfg, train_eval_zarr, rasterizer)

# Validation Dataset
eval_cfg = cfg["val_data_loader"]
eval_zarr = ChunkedDataset(dm.require(eval_cfg["key"])).open()
eval_dataset = EgoDataset(cfg, eval_zarr, rasterizer)
# For evaluation
num_scenes_to_unroll = eval_cfg["max_scene_id"]
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# Load train data
train_cfg = cfg["train_data_loader"]
train_scheme = train_cfg["scheme"]
# max_scene_id = train_cfg["max_scene_id"]
num_epochs = train_cfg["epochs"]

dro_loss_computer = None
if train_scheme == 'group_dro':
    dro_loss_computer = LossComputer(num_groups, group_counts, group_str, device, logger)
elif train_scheme == 'vrex':
    dro_loss_computer = VRexLossComputer(num_groups, group_counts, group_str, device, logger)

# Planning Model
model = RasterizedPlanningModel(
    model_arch="simple_cnn",
    num_input_channels=rasterizer.num_channels(),
    num_targets=3 * cfg["model_params"]["future_num_frames"],  # X, Y, Yaw * number of future states
    weights_scaling=[1., 1., 1.],
    criterion=nn.MSELoss(reduction="none"),
    dro_loss_computer=dro_loss_computer)


# Sub-sample
train_dataset = subset_and_subsample(train_dataset_original, ratio=train_cfg['ratio'], step=train_cfg['step'])

sampler = None
if train_scheme in {'weighted_sampling', 'group_dro', 'vrex'}:
    sample_weights = get_sample_weights(scene_type_to_id_dict, cumulative_sizes, ratio=train_cfg['ratio'], step=train_cfg['step'])
    sampler = torch.utils.data.WeightedRandomSampler(sample_weights, len(train_dataset))
    train_cfg["shuffle"] = False

# Reproducibility of Dataloader
def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)

g = torch.Generator()
g.manual_seed(seed)
train_dataloader = DataLoader(train_dataset, shuffle=train_cfg["shuffle"], batch_size=train_cfg["batch_size"],
                              num_workers=train_cfg["num_workers"], sampler=sampler, worker_init_fn=seed_worker,
                              generator=g)

model = model.to(device)
# optimizer = optim.SGD(
#     filter(lambda p: p.requires_grad, model.parameters()),
#            lr=5e-4,
#            momentum=0.9,
#            weight_decay=train_cfg["w_decay"])
optimizer = optim.Adam(model.parameters(), lr=5e-4, weight_decay=train_cfg["w_decay"])

if train_cfg["scheduler"] == "one_cycle":
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        epochs=num_epochs,
        steps_per_epoch=len(train_dataloader),
        max_lr=5e-4,
        pct_start=0.3)
else:
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer,
        step_size=len(train_dataloader) * num_epochs + 1,
        gamma=0.1)

# Train
model.train()
torch.set_grad_enabled(True)


import time
start = time.time()
total_steps = 0
for epoch in range(train_cfg['epochs']):
    for data in tqdm(train_dataloader):
        total_steps += 1
        # Append Reward scaling
        if train_scheme == 'weighted_reward':
            data = append_reward_scaling(data, reward_scale, scene_id_to_type_list)

        # Append Group Index
        if train_scheme in {'group_dro', 'vrex'}:
            data = append_group_index(data, group_str, scene_id_to_type_list)

        # Forward pass
        data = {k: v.to(device) for k, v in data.items()}

        result = model(data)
        loss = result["loss"]
        # Backward pass
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        logger.record('rollout/loss', loss.item())
        logger.record('rollout/lr', scheduler.get_last_lr()[0])
        logger.dump(total_steps)

    # Eval
    if (epoch + 1) % cfg["train_params"]["eval_every_n_epochs"] == 0:
        eval_model(model, train_eval_dataset, logger, "train", total_steps, num_scenes_to_unroll,
                   enable_scene_type_aggregation=True, scene_id_to_type_path=scene_id_to_type_path)
        eval_model(model, eval_dataset, logger, "eval", total_steps, num_scenes_to_unroll,
                   enable_scene_type_aggregation=True, scene_id_to_type_path=scene_id_to_type_path)
        model.train()

    # Checkpoint
    if (epoch + 1) % cfg["train_params"]["checkpoint_every_n_epochs"] == 0:
        to_save = torch.jit.script(model.cpu())
        path_to_save = str(save_path / f"{output_name}_{total_steps}_steps.pt")
        to_save.save(path_to_save)
        model = model.to(device)

print("Time: ", time.time() - start)

# Final Eval
# eval_model(model, train_eval_dataset, logger, "train", total_steps, num_scenes_to_unroll,
#            enable_scene_type_aggregation=True, scene_id_to_type_path=scene_id_to_type_path)
eval_model(model, eval_dataset, logger, "eval", total_steps, num_scenes_to_unroll=4000,
           enable_scene_type_aggregation=True, scene_id_to_type_path=scene_id_to_type_path)

# Final Checkpoint
to_save = torch.jit.script(model.cpu())
path_to_save = str(save_path / f"{output_name}_{total_steps}_steps.pt")
to_save.save(path_to_save)
model = model.to(device)