import datetime
import math
import time
from tqdm import tqdm
from pathlib import Path

import colossalai
import timm
import timm.optim.optim_factory as optim_factory
import torch
import torchvision.datasets as datasets
from colossalai.context import Config
from colossalai.core import global_context as gpc
from colossalai.logging import get_dist_logger
from colossalai.utils import get_dataloader
from timm.utils import accuracy
from torchvision import transforms

import models_mae
import util.lr_sched as lr_sched
from timm.utils import accuracy
import util.misc as misc
from deit_helper import load_model_args, lr_sched_args
from util.misc import NativeScalerWithGradNormCount as NativeScaler

assert timm.__version__ == "0.3.2"  # version check


# global states
LOGGER = get_dist_logger()
VERBOSE = False


def _load_imgfolder(path, transform):
    return datasets.ImageFolder(path, transform=transform)


def model(norm_pix_loss):
    m = models_mae.mae_vit_large_patch16(norm_pix_loss=norm_pix_loss)
    if VERBOSE:
        LOGGER.info("Use model vit_large_patch16")
    return m


def criterion():
    c = torch.nn.CrossEntropyLoss()
    if VERBOSE:
        LOGGER.info(f"Criterion:\n{c}")
    return c


def optimizer(model, learning_rate, weight_decay):
    # following timm: set wd as 0 for bias and norm layers
    param_groups = optim_factory.add_weight_decay(model, weight_decay)
    o = torch.optim.AdamW(param_groups, lr=learning_rate, betas=(0.9, 0.95))
    if VERBOSE:
        LOGGER.info(f"Optimizer:\n{o}")
    return o


def pretrain_dataloaders(
    datapath: Path,
    transform_train: transforms.Compose,
    transform_val: transforms.Compose,
):
    if VERBOSE:
        LOGGER.info(f"DATAPATH: {datapath.absolute()}")
    train_dataset = _load_imgfolder(datapath / "train", transform_train)
    test_dataset = _load_imgfolder(datapath / "val", transform_val)
    if VERBOSE:
        LOGGER.info(f"Train dataset:\n{train_dataset}")
        LOGGER.info(f"Test dataset:\n{test_dataset}")

    train_dataloader = get_dataloader(
        dataset=train_dataset,
        shuffle=True,
        batch_size=gpc.config.BATCH_SIZE,
        num_workers=1,
        pin_memory=True,
        drop_last=True,
    )

    test_dataloader = get_dataloader(
        dataset=train_dataset,
        shuffle=True,
        batch_size=gpc.config.BATCH_SIZE,
        num_workers=1,
        pin_memory=True,
        drop_last=False,
    )

    return train_dataloader, test_dataloader


def init_global_states(config: Config):
    global VERBOSE
    VERBOSE = config.VERBOSE


def init_engine(config: Config):
    _model = model(config.NORM_PIX_LOSS)
    _optimizer = optimizer(_model, config.LEARNING_RATE, config.WEIGHT_DECAY)
    _criterion = criterion()
    train_dataloader, test_dataloader = pretrain_dataloaders(
        config.DATAPATH, config.TRANSFORM_TRAIN, config.TRANSFORM_VAL
    )
    engine, train_dataloader, test_dataloader, _ = colossalai.initialize(
        _model,
        _optimizer,
        _criterion,
        train_dataloader,
        test_dataloader,
    )
    return engine, train_dataloader, test_dataloader


def resume_model(engine, loss_scaler, config):
    args = load_model_args(
        resume=config.RESUME_ADDRESS, start_epoch=config.RESUME_START_EPOCH
    )

    misc.load_model(
        args=args,
        model_without_ddp=engine.model,
        optimizer=engine.optimizer,
        loss_scaler=loss_scaler,
    )
    if VERBOSE:
        LOGGER.info(
            f"Resume model from {config.RESUME_ADDRESS}, start at epoch {config.RESUME_START_EPOCH}"
        )

    return args.start_epoch


def adjust_learning_rate(engine, data_iter_step, train_dataloader, epoch, config):
    args = lr_sched_args(
        lr=config.LEARNING_RATE,
        min_lr=config.MINIMUM_LEARNING_RATE,
        epochs=config.NUM_EPOCHS,
        warmup_epochs=config.WARMUP_EPOCHS,
    )
    lr_sched.adjust_learning_rate(
        engine.optimizer,
        data_iter_step / len(train_dataloader) + epoch,
        args,
    )


def exit_if_infinite_loss(l):
    if not math.isfinite(l):
        print("Loss is {}, stopping training".format(l))
        sys.exit(1)


def scale_loss(engine, loss, loss_scaler, data_iter_step, config):
    loss /= config.ACCUM_ITER
    loss_scaler(
        loss,
        engine.optimizer,
        parameters=engine.model.parameters(),
        update_grad=(data_iter_step + 1) % config.ACCUM_ITER == 0,
    )


def save_model(model, output_dir, epoch, config, optimizer, loss_scaler):
    checkpoint_path = output_dir / (f"checkpoint-{epoch}.pth")
    torch.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "epoch": epoch,
            "scaler": loss_scaler.state_dict(),
            "config": config,
        },
        checkpoint_path,
    )


def main(config_path):
    colossalai.launch_from_torch(config_path)
    config = gpc.config

    init_global_states(config)
    engine, train_dataloader, test_dataloader = init_engine(config)
    loss_scaler = NativeScaler()

    start_epoch = 0
    if config.RESUME:
        start_epoch = resume_model(engine, loss_scaler, config)

    LOGGER.info(f"Start pre-training for {config.NUM_EPOCHS} epochs")
    start_time = time.time()
    for epoch in range(start_epoch, config.NUM_EPOCHS):
        engine.train()
        engine.zero_grad()
        for data_iter_step, (samples, _) in enumerate(tqdm(train_dataloader)):
            # TODO: This part could be more "colossal-native", like construct a correct `engine.criterion`.

            # we use a per iteration (instead of per epoch) lr scheduler
            if data_iter_step % config.ACCUM_ITER == 0:
                adjust_learning_rate(
                    engine, data_iter_step, train_dataloader, epoch, config
                )
            samples = samples.cuda()
            loss, _, _ = engine.model(samples, mask_ratio=config.MASK_RATIO)
            loss_value = loss.item()
            exit_if_infinite_loss(loss_value)
            scale_loss(engine, loss, loss_scaler, data_iter_step, config)
            if (data_iter_step + 1) % config.ACCUM_ITER == 0:
                engine.zero_grad()

        if config.OUTPUT_DIR and (
            epoch % config.CHECKPOINT_INTERVAL == 0 or epoch + 1 == config.NUM_EPOCHS
        ):
            save_model(
                engine.model,
                config.OUTPUT_DIR,
                epoch,
                config,
                engine.optimizer,
                loss_scaler,
            )

    # TODO: save model
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    LOGGER.info(f"Training time {total_time_str}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        config = Path(__file__).parent / "config" / "pretrain.py"
    else:
        config = sys.argv[1]
    main(config)
