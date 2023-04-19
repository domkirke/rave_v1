from ast import arg
import torch
from torch.utils.data import DataLoader, random_split
import multiprocessing


from rave.model import RAVE
from rave.core import random_phase_mangle, EMAModelCheckPoint
from rave.core import search_for_run
from rave.multichannel_loader import MCDataset, RandomCrop, Dequantize, simple_audio_preprocess

from effortless_config import Config, setting
import pytorch_lightning as pl
from os import environ, path
import numpy as np

import GPUtil as gpu
from udls.transforms import Compose, RandomApply

if __name__ == "__main__":

    class args(Config):
        groups = ["small", "large"]

        DATA_SIZE = 16
        IN_CHANNELS = 1
        CAPACITY = setting(default=64, small=32, large=64)
        LATENT_SIZE = 128
        RATIOS = setting(
            default=[4, 4, 4, 2],
            small=[4, 4, 4, 2],
            large=[4, 4, 2, 2, 2],
        )
        BIAS = True
        NO_LATENCY = False

        MIN_KL = 1e-4
        MAX_KL = 1e-1
        CROPPED_LATENT_SIZE = 0

        LOUD_STRIDE = 1

        USE_NOISE = True
        NOISE_RATIOS = [4, 4, 4]
        NOISE_BANDS = 5

        D_CAPACITY = 16
        D_MULTIPLIER = 4
        D_N_LAYERS = 4

        WARMUP = 1000000
        MODE = "hinge"
        CKPT = None

        PREPROCESSED = None
        WAV = None
        SR = 48000
        N_SIGNAL = 65536
        MAX_STEPS = 2000000000
        NUM_WORKERS = 0

        BATCH = 8

        NAME = None

    args.parse_args()

    assert args.NAME is not None

    model = RAVE(
        data_size=args.DATA_SIZE,
        capacity=args.CAPACITY,
        latent_size=args.LATENT_SIZE,
        ratios=args.RATIOS,
        bias=args.BIAS,
        loud_stride=args.LOUD_STRIDE,
        use_noise=args.USE_NOISE,
        noise_ratios=args.NOISE_RATIOS,
        noise_bands=args.NOISE_BANDS,
        d_capacity=args.D_CAPACITY,
        d_multiplier=args.D_MULTIPLIER,
        d_n_layers=args.D_N_LAYERS,
        warmup=args.WARMUP,
        mode=args.MODE,
        in_channels=args.IN_CHANNELS,
        no_latency=args.NO_LATENCY,
        sr=args.SR,
        min_kl=args.MIN_KL,
        max_kl=args.MAX_KL,
        cropped_latent_size=args.CROPPED_LATENT_SIZE,
    )

    x = torch.zeros(args.BATCH, args.IN_CHANNELS, 2**14)
    model.validation_step(x, 0)

    dataset = MCDataset(
        args.PREPROCESSED,
        args.WAV,
        preprocess_function=simple_audio_preprocess(args.SR,
                                                    2 * args.N_SIGNAL, 
                                                    n_channels=int(args.IN_CHANNELS)),
        split_set="full",
        transforms=Compose([
            RandomCrop(args.N_SIGNAL),
            RandomApply(
                lambda x: random_phase_mangle(x, 20, 2000, .99, args.SR),
                p=.8,
            ),
            Dequantize(16),
            lambda x: x.astype(np.float32),
        ]),
    )

    val = int(len(dataset) * 0.2) 
    train = len(dataset) - val
    train, val = random_split(dataset, [train, val])

    train = DataLoader(train, args.BATCH, True, drop_last=True, num_workers=int(args.NUM_WORKERS))
    val = DataLoader(val, args.BATCH, False, num_workers=int(args.NUM_WORKERS))

    # CHECKPOINT CALLBACKS
    validation_checkpoint = pl.callbacks.ModelCheckpoint(
        monitor="validation",
        filename="best",
        save_last=True
    )
    last_checkpoint = pl.callbacks.ModelCheckpoint(filename="last")
    ema_checkpoint = EMAModelCheckPoint(model,
                                        filename="ema",
                                        monitor="validation")

    CUDA = gpu.getAvailable(maxMemory=.05)
    VISIBLE_DEVICES = environ.get("CUDA_VISIBLE_DEVICES", "")

    if VISIBLE_DEVICES:
        use_gpu = int(int(VISIBLE_DEVICES) >= 0)
    elif len(CUDA):
        environ["CUDA_VISIBLE_DEVICES"] = str(CUDA[0])
        use_gpu = 1
    elif torch.cuda.is_available():
        print("Cuda is available but no fully free GPU found.")
        print("Training may be slower due to concurrent processes.")
        use_gpu = 1
    else:
        print("No GPU found.")
        use_gpu = 0

    val_check = {}
    if len(train) >= 10000:
        val_check["val_check_interval"] = 10000
    else:
        nepoch = 10000 // len(train)
        val_check["check_val_every_n_epoch"] = nepoch


    if args.CKPT:
        model_path = search_for_run(args.CKPT)
        model = RAVE.load_from_checkpoint(model_path, strict=False)
        model.warmup = args.WARMUP

    trainer = pl.Trainer(
        logger=pl.loggers.TensorBoardLogger(path.join("runs", args.NAME),
                                            name="rave"),
        gpus=use_gpu,
        callbacks=[validation_checkpoint],
        #           last_checkpoint],  #, ema_checkpoint],
        #resume_from_checkpoint=search_for_run(args.CKPT),
        max_epochs=1000000,
        max_steps=args.MAX_STEPS,
        **val_check,
    )
    trainer.fit(model, train, val)
