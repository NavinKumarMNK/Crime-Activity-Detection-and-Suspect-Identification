'@Author:NavinKumarMNK'
# Add the parent directory to the path
import sys
import os
if os.path.abspath('../../') not in sys.path:
    sys.path.append(os.path.abspath('../../'))
import utils.utils as utils

# Import the required modules
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from torchvision import models
import wandb
import torchmetrics
import torch.nn as nn
import pytorch_lightning as pl
from models.EfficientNetb3.Encoder import EfficientNetb3Encoder
from models.LSTM.Decoder import LSTMDecoder
import ray_lightning as rl
from models.LSTM.LSTMDataset import LSTMDataset

class EncoderDecoder(pl.LightningDataModule):
    def __init__(self, 
                    ) -> None:
        super(EncoderDecoder, self).__init__()
        self.example_input_array = torch.zeros(1, 3, 256, 256)
        self.save_hyperparameters()
        self.encoder = EfficientNetb3Encoder()
        self.best_val_loss = None
        # encoder is freeze no change in weights
        for param in self.encoder.parameters():
            param.requires_grad = False
        self.decoder = LSTMDecoder()
    
    def forward(self, x):
        x = self.encoder(x)
        x = self.decoder(x)
        return x

    def training_step(self, batch, batch_idx):
        x, y = batch
        seq_length = x.shape[1]
        loss = 0
        self.decoder.init_hidden()
        for x in range(seq_length):
            y_hat = self(x.view(-1, 3, 256, 256))
            loss += nn.CrossEntropyLoss()(y_hat, y)
            loss = loss/seq_length
        self.log('train_loss', loss)
        return loss

    def training_epoch_end(self, outputs):
        loss, y_hat, y = outputs["loss"], outputs["y_hat"], outputs["y"]
        avg_loss = torch.stack([x['loss'] for x in loss]).mean()
        self.log('train/loss_epoch', avg_loss)
        self.log('train/acc_epoch', torchmetrics.functional.accuracy(y_hat, y))
        
    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = nn.MSELoss()(y_hat, y)
        self.log('val_loss', loss)
    
    def validation_epoch_end(self, outputs)-> None:
        loss, y_hat, y = outputs["loss"], outputs["y_hat"], outputs["y"]
        avg_loss = torch.stack([x['loss'] for x in loss]).mean()
        self.log('val/loss_epoch', avg_loss)
        self.log('val/acc_epoch', torchmetrics.functional.accuracy(y_hat, y))
        # validation loss is less than previous epoch then save the model
        if (self.best_val_loss) == None:
            self.best_val_loss = avg_loss
            self.save_model()
        elif (avg_loss < self.best_val_loss):
            self.best_val_loss = avg_loss
            self.save_model()

    def save_model(self):
        self.decoder.save_model()
        artifact = wandb.Artifact('lrcn_model.cpkt', type='model')
        wandb.run.log_artifact(artifact)

    def print_params(self): 
        print("Model Parameters:")
        for name, param in self.named_parameters():
            if param.requires_grad:
                print(name, param.data.shape)

    def test_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = nn.MSELoss()(y_hat, y)
        self.log('test_loss', loss)
    
    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=0.001)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.1)
        return {
            'optimizer': optimizer,
            'lr_scheduler': lr_scheduler
        }
        
    def prediction_step(self, batch, batch_idx, dataloader_idx=None):
        x, y = batch
        y_hat = self(x)
        return y_hat
    
if __name__ == '__main__' :
    from pytorch_lightning.loggers import WandbLogger
    logger = WandbLogger(project='CrimeDetection', name='Encoder-Decoder')

    import wandb
    wandb.init()
    from pytorch_lightning import Trainer
    from torch.utils.data import DataLoader

    dataset_params = utils.config_parse('LSTM_DATASET')
    dataset = LSTMDataset(**dataset_params)

    train_dataloader = DataLoader(dataset, batch_size=1, num_workers=4, shuffle=True)
    val_dataloader = DataLoader(dataset, batch_size=1, num_workers=4, shuffle=True)
    test_dataloader = DataLoader(dataset, batch_size=1, num_workers=4, shuffle=True)

    from pytorch_lightning.callbacks import ModelSummary
    from pytorch_lightning.callbacks.progress import TQDMProgressBar
    from pytorch_lightning.callbacks import ModelCheckpoint
    from pytorch_lightning.callbacks import EarlyStopping
    from pytorch_lightning.callbacks.device_stats_monitor import DeviceStatsMonitor

    early_stopping = EarlyStopping(monitor='val_loss', patience=5)
    device_monitor = DeviceStatsMonitor()
    checkpoint_callback = ModelCheckpoint(dirpath=utils.ROOT_PATH + '/weights/checkpoints/autoencoder/')
    model_summary = ModelSummary(max_depth=3)
    refresh_rate = TQDMProgressBar(refresh_rate=10)

    callbacks = [
        model_summary,
        refresh_rate,
        checkpoint_callback,
        early_stopping,
        device_monitor
    ]

    model = EncoderDecoder()
    ed_params = utils.config_parse('ENCODER_DECODER_TRAIN')
    print(ed_params)
    
    dist_env_params = utils.config_parse('DISTRIBUTED_ENV')
    strategy = None
    if int(dist_env_params['horovod']) == 1:
        strategy = rl.HorovodRayStrategy(num_workers=dist_env_params['num_workers'],
                                        use_gpu=dist_env_params['use_gpu'])
    elif int(dist_env_params['model_parallel']) == 1:
        strategy = rl.RayShardedStrategy(num_workers=dist_env_params['num_workers'],
                                        use_gpu=dist_env_params['use_gpu'])
    elif int(dist_env_params['data_parallel']) == 1:
        strategy = rl.RayStrategy(num_workers=dist_env_params['num_workers'],
                                        use_gpu=dist_env_params['use_gpu'])
    trainer = Trainer(**ed_params, 
                    callbacks=callbacks, 
                    logger=logger,
                    strategy=strategy)
    trainer.fit(model, train_dataloader, val_dataloader)

    model.decoder.finalize()

    