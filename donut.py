"""
pip install -q transformers datasets sentencepiece
pip install -q pytorch-lightning wandb
pip install sconf
pip install donut-python
pip install pytorch-lightning==2.0.7
pip install timm==0.5.4
pip install transformers
pip install pytorch-lightning==1.6.4
pip install transformers==4.11.3
pip install numpy
pip install donut
"""
CONFIG_NAME = "train_cord_finetune.yaml"
CONFIG_PATH = f"/home/ec2-user/{CONFIG_NAME}" 
#train.py file from Donut
import argparse
import datetime
import json
import os
import random
from io import BytesIO
from os.path import basename
from pathlib import Path
import torch.nn as nn
import numpy as np
import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.loggers.tensorboard import TensorBoardLogger
from pytorch_lightning.plugins import CheckpointIO
from pytorch_lightning.utilities import rank_zero_only
from sconf import Config

from donut import DonutDataset
from lightning_module import DonutDataPLModule, DonutModelPLModule


class CustomCheckpointIO(CheckpointIO):
    def save_checkpoint(self, checkpoint, path, storage_options=None):
        if (checkpoint.get("state_dict", None) is not None):
          del checkpoint["state_dict"]
        torch.save(checkpoint, path)

    def load_checkpoint(self, path, storage_options=None):
        checkpoint = torch.load(path + "artifacts.ckpt")
        state_dict = torch.load(path + "pytorch_model.bin")
        checkpoint["state_dict"] = {"model." + key: value for key, value in state_dict.items()}
        return checkpoint

    def remove_checkpoint(self, path) -> None:
        return super().remove_checkpoint(path)


@rank_zero_only
def save_config_file(config, path):
    if not Path(path).exists():
        os.makedirs(path)
    save_path = Path(path) / "config.yaml"
    print(config.dumps())
    with open(save_path, "w") as f:
        f.write(config.dumps(modified_color=None, quote_str=True))
        print(f"Config is saved at {save_path}")


class ProgressBar(pl.callbacks.TQDMProgressBar):
    def __init__(self, config):
        super().__init__()
        self.enable = True
        self.config = config

    def disable(self):
        self.enable = False

    def get_metrics(self, trainer, model):
        items = super().get_metrics(trainer, model)
        items.pop("v_num", None)
        items["exp_name"] = f"{self.config.get('exp_name', '')}"
        items["exp_version"] = f"{self.config.get('exp_version', '')}"
        return items


def set_seed(seed):
    pytorch_lightning_version = int(pl.__version__[0])
    if pytorch_lightning_version < 2:
        pl.utilities.seed.seed_everything(seed, workers=True)
    else:
        import lightning_fabric
        lightning_fabric.utilities.seed.seed_everything(seed, workers=True)


def train(config):
    set_seed(config.get("seed", 42))

    model_module = DonutModelPLModule(config)
    data_module = DonutDataPLModule(config)

    # add datasets to data_module
    datasets = {"train": [], "validation": []}
    for i, dataset_name_or_path in enumerate(config.dataset_name_or_paths):
        task_name = os.path.basename(dataset_name_or_path)  # e.g., cord-v2, docvqa, rvlcdip, ...

        # add categorical special tokens (optional)
        if task_name == "rvlcdip":
            model_module.model.decoder.add_special_tokens([
                "<advertisement/>", "<budget/>", "<email/>", "<file_folder/>",
                "<form/>", "<handwritten/>", "<invoice/>", "<letter/>",
                "<memo/>", "<news_article/>", "<presentation/>", "<questionnaire/>",
                "<resume/>", "<scientific_publication/>", "<scientific_report/>", "<specification/>"
            ])
        if task_name == "docvqa":
            model_module.model.decoder.add_special_tokens(["<yes/>", "<no/>"])

        for split in ["train", "validation"]:
            datasets[split].append(
                DonutDataset(
                    dataset_name_or_path=dataset_name_or_path,
                    donut_model=model_module.model,
                    max_length=config.max_length,
                    split=split,
                    task_start_token=config.task_start_tokens[i]
                    if config.get("task_start_tokens", None)
                    else f"<s_{task_name}>",
                    prompt_end_token="<s_answer>" if "docvqa" in dataset_name_or_path else f"<s_{task_name}>",
                    sort_json_key=config.sort_json_key,
                )
            )
            # prompt_end_token is used for ignoring a given prompt in a loss function
            # for docvqa task, i.e., {"question": {used as a prompt}, "answer": {prediction target}},
            # set prompt_end_token to "<s_answer>"

    data_module.train_datasets = datasets["train"]
    data_module.val_datasets = datasets["validation"]

    logger = TensorBoardLogger(
        save_dir=config.result_path,
        name=config.exp_name,
        version=config.exp_version,
        default_hp_metric=False,
    )

    lr_callback = LearningRateMonitor(logging_interval="step")

    checkpoint_callback = ModelCheckpoint(
        monitor="val_metric",
        dirpath=Path(config.result_path) / config.exp_name / config.exp_version,
        filename="artifacts",
        save_top_k=1,
        save_last=False,
        mode="min",
    )

    bar = ProgressBar(config)

    custom_ckpt = CustomCheckpointIO()
    trainer = pl.Trainer(
        num_nodes=config.get("num_nodes", 1),
        devices=torch.cuda.device_count(),
        #strategy="ddp",
        accelerator="gpu",
        plugins=custom_ckpt,
        max_epochs=config.max_epochs,
        max_steps=config.max_steps,
        val_check_interval=config.val_check_interval,
        check_val_every_n_epoch=config.check_val_every_n_epoch,
        gradient_clip_val=config.gradient_clip_val,
        precision=32,
        num_sanity_val_steps=0,
        logger=logger,
        callbacks=[lr_callback, checkpoint_callback, bar],
    )

    trainer.fit(model_module, data_module, ckpt_path=config.get("resume_from_checkpoint_path", None))
    trainer.save_checkpoint(f"{Path(config.result_path)}/{config.exp_name}/{config.exp_version}/model_checkpoint.ckpt")


if __name__ == "__main__":

    config = Config(CONFIG_PATH)
    config.argv_update([])

    config.exp_name = basename(CONFIG_NAME).split(".")[0]
    config.exp_version = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    config.dataset_name_or_paths = ["/home/ec2-user/preparedFinetuneData"]
    config.train_batch_sizes = [1]
    config.check_val_every_n_epoch = 10
    config.max_steps = 110 # infinite, since max_epochs is specified
    config.num_workers = 2 #recommended max
    config.num_nodes = 1
    config.pretrained_model_name_or_path= "naver-clova-ix/donut-base-finetuned-cord-v2" #donut finetuned on Cord dataset
    config.warmup_steps = 0
    print("config path:" , Path(config.result_path),config.exp_name, config.exp_version)
    save_config_file(config, Path(config.result_path) / config.exp_name / config.exp_version)
    train(config)
