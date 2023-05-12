import os
import sys

import pytorch_lightning as pl
from torch.utils.data import DataLoader

from lib.dataset import ScannetReferenceDataset
from lib.dataset import get_scanrefer

sys.path.append(os.path.join(os.getcwd(), "lib"))  # HACK add the lib folder


class ScanReferDataModule(pl.LightningDataModule):
    def __init__(self):
        super().__init__()
        self.dataset_val = None
        self.dataset_test = None
        self.dataset_train = None
        self.all_scene_list = None
        self.Scanrefer_eval_val = None
        self.Scanrefer_eval_train = None
        self.Scanrefer_train = None

    def prepare_data(self):
        self.Scanrefer_train, self.Scanrefer_eval_train, self.Scanrefer_eval_val, self.all_scene_list = get_scanrefer(
            model='')

    def setup(self, stage: str):
        if stage == 'fit':
            self.dataset_train = ScannetReferenceDataset(
                scanrefer=self.Scanrefer_train,
                scanrefer_all_scene=self.all_scene_list,
                split='train',
                num_points=40000,
                augment=False,
            )
        if stage == 'validate':
            self.dataset_val = ScannetReferenceDataset(
                scanrefer=self.Scanrefer_eval_train,
                scanrefer_all_scene=self.all_scene_list,
                split='train',
                num_points=40000,
                augment=False,
            )
        if stage == 'test':
            self.dataset_test = ScannetReferenceDataset(
                scanrefer=self.Scanrefer_eval_val,
                scanrefer_all_scene=self.all_scene_list,
                split='train',
                num_points=40000,
                augment=False,
            )

    def train_dataloader(self):
        return DataLoader(self.dataset_train, batch_size=1, shuffle=True, num_workers=4,
                          collate_fn=self.dataset_train.collate_fn)

    def val_dataloader(self):
        return DataLoader(self.dataset_val, batch_size=4, shuffle=True, num_workers=4,
                          collate_fn=self.dataset_val.collate_fn)

    def test_dataloader(self):
        return DataLoader(self.dataset_test, batch_size=4, shuffle=True, num_workers=4,
                          collate_fn=self.dataset_test.collate_fn)
