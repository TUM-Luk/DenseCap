'''
File Created: Monday, 25th November 2019 1:35:30 pm
Author: Dave Zhenyu Chen (zhenyu.chen@tum.de)
'''

import os
import sys
import time
import h5py
import json
import pickle
import numpy as np
import multiprocessing as mp
import torch
import scipy.interpolate
import scipy.ndimage
import math

from itertools import chain
from collections import Counter
from torch.utils.data import Dataset
from ops import voxelization_idx

sys.path.append(os.path.join(os.getcwd(), "lib"))  # HACK add the lib folder
from lib.config import CONF
from utils.pc_utils import random_sampling, rotx, roty, rotz
from utils.box_util import get_3d_box, get_3d_box_batch
from data.scannet.model_util_scannet import rotate_aligned_boxes, ScannetDatasetConfig, rotate_aligned_boxes_along_axis

from copy import deepcopy

# map nyu40id label (1-40) to class label (0-19), wall(0), floor(1), ceiling (-100) are NOT considered in cluster grouping
semantic_map = {
    22: -100, 0: -100,  # ceiling(22), unannotated(0)
    1: 0,  # wall
    2: 1,  # floor
    3: 2,  # cabinet
    4: 3,  # bed
    5: 4,  # chair
    6: 5,  # sofa
    7: 6,  # table
    8: 7,  # door
    9: 8,  # window
    10: 9,  # bookshelf
    11: 10,  # picture
    12: 11,  # counter
    14: 12,  # desk
    16: 13,  # curtain
    24: 14,  # refrigerator
    28: 15,  # shower curtain
    33: 16,  # toilet
    34: 17,  # sink
    36: 18,  # bathtub
    13: 19, 15: 19, 17: 19, 18: 19, 19: 19, 20: 19, 21: 19, 23: 19, 25: 19, 26: 19, 27: 19, 29: 19, 30: 19, 31: 19,
    32: 19, 35: 19, 37: 19, 38: 19, 39: 19, 40: 19  # other furniture

}
# data setting
DC = ScannetDatasetConfig()
MAX_NUM_OBJ = 128
MEAN_COLOR_RGB = np.array([109.8, 97.2, 83.8])

# data path
SCANNET_V2_TSV = os.path.join(CONF.PATH.SCANNET_META, "scannetv2-labels.combined.tsv")
# SCANREFER_VOCAB = os.path.join(CONF.PATH.DATA, "ScanRefer_vocabulary.json")
VOCAB = os.path.join(CONF.PATH.DATA, "{}_vocabulary.json")  # dataset_name
# SCANREFER_VOCAB_WEIGHTS = os.path.join(CONF.PATH.DATA, "ScanRefer_vocabulary_weights.json")
VOCAB_WEIGHTS = os.path.join(CONF.PATH.DATA, "{}_vocabulary_weights.json")  # dataset_name
# MULTIVIEW_DATA = os.path.join(CONF.PATH.SCANNET_DATA, "enet_feats.hdf5")
MULTIVIEW_DATA = CONF.MULTIVIEW
GLOVE_PICKLE = os.path.join(CONF.PATH.DATA, "glove.p")


def get_scanrefer(model=None):
    scanrefer_train = json.load(open(os.path.join(CONF.PATH.DATA, "scanrefer/ScanRefer_filtered_train.json")))
    scanrefer_eval_train = json.load(open(os.path.join(CONF.PATH.DATA, "scanrefer/ScanRefer_filtered_train.json")))
    scanrefer_eval_val = json.load(open(os.path.join(CONF.PATH.DATA, "scanrefer/ScanRefer_filtered_val.json")))

    SCANREFER_TRAIN = json.load(open(os.path.join(CONF.PATH.DATA, "scanrefer/ScanRefer_filtered_train.json")))

    if model == 'debug':
        scanrefer_train = [SCANREFER_TRAIN[0]]
        scanrefer_eval_train = [SCANREFER_TRAIN[0]]
        scanrefer_eval_val = [SCANREFER_TRAIN[0]]

    # get initial scene list
    train_scene_list = sorted(list(set([data["scene_id"] for data in scanrefer_train])))  # 562 train scenes
    val_scene_list = sorted(list(set([data["scene_id"] for data in scanrefer_eval_val])))  # 141 val scenes

    # filter data in chosen scenes
    new_scanrefer_train = []
    for data in scanrefer_train:
        if data["scene_id"] in train_scene_list:
            new_scanrefer_train.append(data)

    # eval on train
    new_scanrefer_eval_train = []
    for scene_id in train_scene_list:
        data = deepcopy(SCANREFER_TRAIN[0])
        data["scene_id"] = scene_id
        new_scanrefer_eval_train.append(data)

    # eval on val
    new_scanrefer_eval_val = []
    for scene_id in val_scene_list:
        data = deepcopy(SCANREFER_TRAIN[0])
        data["scene_id"] = scene_id
        new_scanrefer_eval_val.append(data)

    # for data in scanrefer_eval_val:
    #     if data["scene_id"] in val_scene_list:
    #         new_scanrefer_eval_val.append(data)

    # all scanrefer scene
    all_scene_list = train_scene_list + val_scene_list

    print("using ScanRefer dataset")
    print("train on {} samples from {} scenes".format(len(new_scanrefer_train), len(train_scene_list)))
    print("eval on {} scenes from train and {} scenes from val".format(len(new_scanrefer_eval_train),
                                                                       len(new_scanrefer_eval_val)))
    return new_scanrefer_train, new_scanrefer_eval_train, new_scanrefer_eval_val, all_scene_list


class ReferenceDataset(Dataset):
    def __init__(self):
        pass

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, idx):
        raise NotImplementedError

    # 获得raw_category(物体名字)与label_id的对应字典
    # 例：raw2label中的“chair“对应label_id为2，”window“对应6
    def _get_raw2label(self):
        # mapping
        scannet_labels = DC.type2class.keys()
        scannet2label = {label: i for i, label in enumerate(scannet_labels)}

        lines = [line.rstrip() for line in open(SCANNET_V2_TSV)]
        lines = lines[1:]
        raw2label = {}
        for i in range(len(lines)):
            label_classes_set = set(scannet_labels)
            elements = lines[i].split('\t')
            raw_name = elements[1]
            nyu40_name = elements[7]
            if nyu40_name not in label_classes_set:
                raw2label[raw_name] = scannet2label['others']
            else:
                raw2label[raw_name] = scannet2label[nyu40_name]

        return raw2label

    # 返回一个字典，unique_multiple_lookup[scene_id][object_id][ann_id]=0或1
    # 0表示该object种类在场景中不止一个，例如object_id=39的物体是一个cabinet，若场景中有不止一个cabinet，
    # 则unique_multiple_lookup[scene0001_00][39][ann_id]=1
    def _get_unique_multiple_lookup(self):
        all_sem_labels = {}
        cache = {}
        for data in self.scanrefer:
            scene_id = data["scene_id"]
            object_id = data["object_id"]
            object_name = " ".join(data["object_name"].split("_"))
            ann_id = data["ann_id"]

            if scene_id not in all_sem_labels:
                all_sem_labels[scene_id] = []

            if scene_id not in cache:
                cache[scene_id] = {}

            if object_id not in cache[scene_id]:
                cache[scene_id][object_id] = {}
                try:
                    all_sem_labels[scene_id].append(self.raw2label[object_name])
                except KeyError:
                    all_sem_labels[scene_id].append(17)

        # convert to numpy array
        all_sem_labels = {scene_id: np.array(all_sem_labels[scene_id]) for scene_id in all_sem_labels.keys()}

        unique_multiple_lookup = {}
        for data in self.scanrefer:
            scene_id = data["scene_id"]
            object_id = data["object_id"]
            object_name = " ".join(data["object_name"].split("_"))
            ann_id = data["ann_id"]

            try:
                sem_label = self.raw2label[object_name]
            except KeyError:
                sem_label = 17

            unique_multiple = 0 if (all_sem_labels[scene_id] == sem_label).sum() == 1 else 1

            # store
            if scene_id not in unique_multiple_lookup:
                unique_multiple_lookup[scene_id] = {}

            if object_id not in unique_multiple_lookup[scene_id]:
                unique_multiple_lookup[scene_id][object_id] = {}

            if ann_id not in unique_multiple_lookup[scene_id][object_id]:
                unique_multiple_lookup[scene_id][object_id][ann_id] = None

            unique_multiple_lookup[scene_id][object_id][ann_id] = unique_multiple

        return unique_multiple_lookup

    # 返回两个字典lang和lang_ids
    # lang[scene_id][object_id][ann_id]存储每个句子embedding后的词向量
    # lang_ids[scene_id][object_id][ann_id]存储每个句子中单词对应idx的列表
    def _tranform_des(self):
        lang = {}
        label = {}
        for data in self.scanrefer:
            scene_id = data["scene_id"]
            object_id = data["object_id"]
            ann_id = data["ann_id"]

            if scene_id not in lang:
                lang[scene_id] = {}
                label[scene_id] = {}

            if object_id not in lang[scene_id]:
                lang[scene_id][object_id] = {}
                label[scene_id][object_id] = {}

            if ann_id not in lang[scene_id][object_id]:
                lang[scene_id][object_id][ann_id] = {}
                label[scene_id][object_id][ann_id] = {}

            # trim long descriptions
            # 若小于最大长度，则tokens列表长度不变，即不会再后面多加空的元素
            tokens = data["token"][:CONF.TRAIN.MAX_DES_LEN]

            # tokenize the description
            tokens = ["sos"] + tokens + ["eos"]
            embeddings = np.zeros((CONF.TRAIN.MAX_DES_LEN + 2, 300))  # word embedding后该句子中每个单词的词向量
            labels = np.zeros((CONF.TRAIN.MAX_DES_LEN + 2))  # start and end，句子中每个单词对应的idx的列表

            # load
            # 若长度未到MAX+2，则embeddings和labels中剩余为0
            # 在word2idx中，pad_对应的idx为0
            for token_id in range(len(tokens)):
                token = tokens[token_id]
                try:
                    embeddings[token_id] = self.glove[token]
                    labels[token_id] = self.vocabulary["word2idx"][token]
                except KeyError:
                    embeddings[token_id] = self.glove["unk"]
                    labels[token_id] = self.vocabulary["word2idx"]["unk"]

            # store
            lang[scene_id][object_id][ann_id] = embeddings
            label[scene_id][object_id][ann_id] = labels

        return lang, label

    # self.vocabulary 根据glove文件存储了该dataset中description中出现的单词
    # word2idx字典由word得到idx
    # idx2word字典由idx得到word
    # 其中加入了四个特殊token["pad_", "unk", "sos", "eos"],分别对应idx的0，1，2，3
    def _build_vocabulary(self, dataset_name):
        vocab_path = VOCAB.format(dataset_name)
        if os.path.exists(vocab_path):
            self.vocabulary = json.load(open(vocab_path))
        else:
            if self.split == "train":
                all_words = chain(*[data["token"][:CONF.TRAIN.MAX_DES_LEN] for data in self.scanrefer])
                word_counter = Counter(all_words)
                word_counter = sorted([(k, v) for k, v in word_counter.items() if k in self.glove], key=lambda x: x[1],
                                      reverse=True)
                word_list = [k for k, _ in word_counter]

                # build vocabulary
                word2idx, idx2word = {}, {}
                spw = ["pad_", "unk", "sos", "eos"]  # NOTE distinguish padding token "pad_" and the actual word "pad"
                for i, w in enumerate(word_list):
                    shifted_i = i + len(spw)
                    word2idx[w] = shifted_i
                    idx2word[shifted_i] = w

                # add special words into vocabulary
                for i, w in enumerate(spw):
                    word2idx[w] = i
                    idx2word[i] = w

                vocab = {
                    "word2idx": word2idx,
                    "idx2word": idx2word
                }
                json.dump(vocab, open(vocab_path, "w"), indent=4)

                self.vocabulary = vocab

    def _build_frequency(self, dataset_name):
        vocab_weights_path = VOCAB_WEIGHTS.format(dataset_name)
        if os.path.exists(vocab_weights_path):
            with open(vocab_weights_path) as f:
                weights = json.load(f)
                self.weights = np.array([v for _, v in weights.items()])
        else:
            all_tokens = []
            for scene_id in self.lang_ids.keys():
                for object_id in self.lang_ids[scene_id].keys():
                    for ann_id in self.lang_ids[scene_id][object_id].keys():
                        all_tokens += self.lang_ids[scene_id][object_id][ann_id].astype(int).tolist()

            word_count = Counter(all_tokens)
            word_count = sorted([(k, v) for k, v in word_count.items()], key=lambda x: x[0])

            # frequencies = [c for _, c in word_count]
            # weights = np.array(frequencies).astype(float)
            # weights = weights / np.sum(weights)
            # weights = 1 / np.log(1.05 + weights)

            weights = np.ones((len(word_count)))

            self.weights = weights

            with open(vocab_weights_path, "w") as f:
                weights = {k: v for k, v in enumerate(weights)}
                json.dump(weights, f, indent=4)

    def _load_data(self, dataset_name):
        print("loading data...")
        # load language features
        self.glove = pickle.load(open(GLOVE_PICKLE, "rb"))
        self._build_vocabulary(dataset_name)  # 存储了word2index和index2word
        self.num_vocabs = len(self.vocabulary["word2idx"].keys())  # 共有多少种words
        self.lang, self.lang_ids = self._tranform_des()  # 表示caption的两个字典，详细见上def备注
        self._build_frequency(dataset_name)

        # add scannet data
        self.scene_list = sorted(list(set([data["scene_id"] for data in self.scanrefer])))

        # load scene data 从预处理文件中读取所有的numpy数组信息并存储在字典里
        self.scene_data = {}
        for scene_id in self.scene_list:
            self.scene_data[scene_id] = {}
            # self.scene_data[scene_id]["mesh_vertices"] = np.load(os.path.join(CONF.PATH.SCANNET_DATA, scene_id)+"_vert.npy")
            self.scene_data[scene_id]["mesh_vertices"] = np.load(
                os.path.join(CONF.PATH.SCANNET_DATA, scene_id) + "_aligned_vert.npy")  # axis-aligned
            self.scene_data[scene_id]["instance_labels"] = np.load(
                os.path.join(CONF.PATH.SCANNET_DATA, scene_id) + "_ins_label.npy")
            self.scene_data[scene_id]["semantic_labels"] = np.load(
                os.path.join(CONF.PATH.SCANNET_DATA, scene_id) + "_sem_label.npy")
            # self.scene_data[scene_id]["instance_bboxes"] = np.load(os.path.join(CONF.PATH.SCANNET_DATA, scene_id)+"_bbox.npy")
            self.scene_data[scene_id]["instance_bboxes"] = np.load(
                os.path.join(CONF.PATH.SCANNET_DATA, scene_id) + "_aligned_bbox.npy")

        # prepare class mapping
        lines = [line.rstrip() for line in open(SCANNET_V2_TSV)]
        lines = lines[1:]
        raw2nyuid = {}
        for i in range(len(lines)):
            elements = lines[i].split('\t')
            raw_name = elements[1]
            nyu40_name = int(elements[4])
            raw2nyuid[raw_name] = nyu40_name

        # store
        self.raw2nyuid = raw2nyuid
        self.raw2label = self._get_raw2label()
        self.unique_multiple_lookup = self._get_unique_multiple_lookup()

    # 对整体点云和bounding box进行平移
    def _translate(self, point_set, bbox):
        # unpack
        coords = point_set[:, :3]

        # translation factors
        x_factor = np.random.choice(np.arange(-0.5, 0.501, 0.001), size=1)[0]
        y_factor = np.random.choice(np.arange(-0.5, 0.501, 0.001), size=1)[0]
        z_factor = np.random.choice(np.arange(-0.5, 0.501, 0.001), size=1)[0]
        factor = [x_factor, y_factor, z_factor]

        # dump
        coords += factor
        point_set[:, :3] = coords
        bbox[:, :3] += factor

        return point_set, bbox


class ScannetReferenceDataset(ReferenceDataset):

    def __init__(self, scanrefer, scanrefer_all_scene,
                 split="train",
                 num_points=40000,
                 augment=False,
                 voxel_cfg=CONF.voxel_cfg):

        # NOTE only feed the scan2cad_rotation when on the training mode and train split

        self.scanrefer = scanrefer
        self.scanrefer_all_scene = scanrefer_all_scene  # all scene_ids in scanrefer 所有scene_id
        self.split = split
        self.num_points = num_points
        self.augment = augment
        self.voxel_cfg = voxel_cfg

        # load data
        self._load_data('Scanrefer')

    def __len__(self):
        return len(self.scanrefer)

    def elastic(self, x, gran, mag):
        blur0 = np.ones((3, 1, 1)).astype('float32') / 3
        blur1 = np.ones((1, 3, 1)).astype('float32') / 3
        blur2 = np.ones((1, 1, 3)).astype('float32') / 3

        bb = np.abs(x).max(0).astype(np.int32) // gran + 3
        noise = [np.random.randn(bb[0], bb[1], bb[2]).astype('float32') for _ in range(3)]
        noise = [scipy.ndimage.filters.convolve(n, blur0, mode='constant', cval=0) for n in noise]
        noise = [scipy.ndimage.filters.convolve(n, blur1, mode='constant', cval=0) for n in noise]
        noise = [scipy.ndimage.filters.convolve(n, blur2, mode='constant', cval=0) for n in noise]
        noise = [scipy.ndimage.filters.convolve(n, blur0, mode='constant', cval=0) for n in noise]
        noise = [scipy.ndimage.filters.convolve(n, blur1, mode='constant', cval=0) for n in noise]
        noise = [scipy.ndimage.filters.convolve(n, blur2, mode='constant', cval=0) for n in noise]
        ax = [np.linspace(-(b - 1) * gran, (b - 1) * gran, b) for b in bb]
        interp = [
            scipy.interpolate.RegularGridInterpolator(ax, n, bounds_error=0, fill_value=0)
            for n in noise
        ]

        def g(x_):
            return np.hstack([i(x_)[:, None] for i in interp])

        return x + g(x) * mag

    def dataAugment(self, xyz, jitter=False, flip=False, rot=False, scale=False, prob=1.0):
        m = np.eye(3)
        if jitter and np.random.rand() < prob:
            m += np.random.randn(3, 3) * 0.1
        if flip and np.random.rand() < prob:
            m[0][0] *= np.random.randint(0, 2) * 2 - 1
        if rot and np.random.rand() < prob:
            theta = np.random.rand() * 2 * math.pi
            m = np.matmul(m, [[math.cos(theta), math.sin(theta), 0],
                              [-math.sin(theta), math.cos(theta), 0], [0, 0, 1]])

        else:
            # Empirically, slightly rotate the scene can match the results from checkpoint
            theta = 0.35 * math.pi
            m = np.matmul(m, [[math.cos(theta), math.sin(theta), 0],
                              [-math.sin(theta), math.cos(theta), 0], [0, 0, 1]])
        if scale and np.random.rand() < prob:
            scale_factor = np.random.uniform(0.95, 1.05)
            xyz = xyz * scale_factor
        return np.matmul(xyz, m)

    def crop(self, xyz, step=32):
        xyz_offset = xyz.copy()
        valid_idxs = xyz_offset.min(1) >= 0
        assert valid_idxs.sum() == xyz.shape[0]
        spatial_shape = np.array([self.voxel_cfg.spatial_shape[1]] * 3)
        room_range = xyz.max(0) - xyz.min(0)
        while (valid_idxs.sum() > self.voxel_cfg.max_npoint):
            step_temp = step
            if valid_idxs.sum() > 1e6:
                step_temp = step * 2
            offset = np.clip(spatial_shape - room_range + 0.001, None, 0) * np.random.rand(3)
            xyz_offset = xyz + offset
            valid_idxs = (xyz_offset.min(1) >= 0) * ((xyz_offset < spatial_shape).sum(1) == 3)
            spatial_shape[:2] -= step_temp
        return xyz_offset, valid_idxs

    def transform_train(self, xyz, rgb, semantic_label, instance_label, aug_prob=1.0):
        if self.augment == True:
            xyz_middle = self.dataAugment(xyz, True, True, True, True, aug_prob)
        else:
            xyz_middle = xyz
        xyz = xyz_middle * self.voxel_cfg.scale
        if np.random.rand() < aug_prob:
            xyz = self.elastic(xyz, 6, 40.)
            xyz = self.elastic(xyz, 20, 160.)
        xyz = xyz - xyz.min(0)
        max_tries = 5
        while (max_tries > 0):
            xyz_offset, valid_idxs = self.crop(xyz)
            if valid_idxs.sum() >= self.voxel_cfg.min_npoint:
                xyz = xyz_offset
                break
            max_tries -= 1
        if valid_idxs.sum() < self.voxel_cfg.min_npoint:
            return None
        xyz = xyz[valid_idxs]
        xyz_middle = xyz_middle[valid_idxs]
        rgb = rgb[valid_idxs]
        semantic_label = semantic_label[valid_idxs]
        instance_label = instance_label[valid_idxs]
        return xyz, xyz_middle, rgb, semantic_label, instance_label

    def transform_test(self, xyz, rgb, semantic_label, instance_label):
        if self.augment == True:
            xyz_middle = self.dataAugment(xyz, False, False, False, False)
        else:
            xyz_middle = xyz
        xyz = xyz_middle * self.voxel_cfg.scale
        xyz -= xyz.min(0)
        valid_idxs = np.ones(xyz.shape[0], dtype=bool)
        instance_label = instance_label[valid_idxs]
        return xyz, xyz_middle, rgb, semantic_label, instance_label

    # instance_num instance的数量（int）
    # instance_pointnum：列表，存储了属于每个instance的点的数量
    # instance_cls:列表，存储了这个object属于的semantic label
    # pt_offset_label:Nx3数组，存储了每个点与该点所属object中心的偏移量
    def getInstanceInfo(self, xyz, instance_label, semantic_label):
        pt_mean = np.ones((xyz.shape[0], 3), dtype=np.float32) * -100.0
        instance_pointnum = []
        instance_cls = []
        # max(instance_num, 0) to support instance_label with no valid instance_id
        instance_num = max(int(instance_label.max()) + 1, 0)
        for i_ in range(instance_num):
            inst_idx_i = np.where(instance_label == i_)
            if inst_idx_i[0].size == 0:  # 因为在前处理时，有些instance比如ceiling已经被删除了，所以可能会有空余的instance_label
                instance_pointnum.append(inst_idx_i[0].size)
                instance_cls.append(-100)
                continue
            xyz_i = xyz[inst_idx_i]  # 对应instance_id为i的所有点坐标
            pt_mean[inst_idx_i] = xyz_i.mean(0)  # 这个instance的所有点的mean
            instance_pointnum.append(inst_idx_i[0].size)
            cls_idx = inst_idx_i[0][0]
            instance_cls.append(semantic_label[cls_idx])
        pt_offset_label = pt_mean - xyz
        return instance_num, instance_pointnum, instance_cls, pt_offset_label

    def __getitem__(self, idx):
        start = time.time()
        scene_id = self.scanrefer[idx]["scene_id"]
        object_id = int(self.scanrefer[idx]["object_id"])
        object_name = " ".join(self.scanrefer[idx]["object_name"].split("_"))  # 把下划线_替换成空格
        ann_id = self.scanrefer[idx]["ann_id"]

        # get language features
        lang_feat = self.lang[scene_id][str(object_id)][ann_id]
        lang_len = len(self.scanrefer[idx]["token"]) + 2
        lang_len = lang_len if lang_len <= CONF.TRAIN.MAX_DES_LEN + 2 else CONF.TRAIN.MAX_DES_LEN + 2

        # get pc 获取前处理的点云数据
        mesh_vertices = self.scene_data[scene_id]["mesh_vertices"]  # nparray float32
        instance_labels = self.scene_data[scene_id]["instance_labels"].astype(np.int64)  # nparray int64
        semantic_labels = self.scene_data[scene_id]["semantic_labels"].astype(np.int64)  # nparray int64
        instance_bboxes = self.scene_data[scene_id]["instance_bboxes"]  # nparray float64

        # 从nyu40id（1-40,0表示未分类）映射到语义分割时的class（0-19）, ceiling和未分配点变为-100
        semantic_labels_nyu40id = np.copy(semantic_labels)  # 保存原始的nyu40id
        for i in range(len(semantic_labels)):
            semantic_labels[i] = semantic_map[semantic_labels[i]]

        # map instance label
        instance_labels = instance_labels - 1
        instance_labels[instance_labels == -1] = -100

        point_cloud = mesh_vertices[:, 0:6]
        # 对坐标数据预处理,让其中心为(0,0,0),目的是为了后面的transform的scale放大操作
        # 对instance_bbox也中心化
        # 获取color数据, 对color正则化，rgb范围为[-1,1]
        point_cloud[:, 0:3] = np.ascontiguousarray(point_cloud[:, 0:3] - point_cloud[:, 0:3].mean(0))
        point_cloud[:, 3:6] = (point_cloud[:, 3:6] - MEAN_COLOR_RGB) / 256.0
        pcl_color = point_cloud[:, 3:6]
        instance_bboxes[:, 0:3] = instance_bboxes[:, 0:3] - point_cloud[:, 0:3].mean(0)

        if self.split == 'train':
            # 随机选取num_points的点，这里是选取40000个点
            point_cloud, choices = random_sampling(point_cloud, self.num_points, return_choices=True)
            instance_labels = instance_labels[choices]
            semantic_labels = semantic_labels[choices]
            semantic_labels_nyu40id = semantic_labels_nyu40id[choices]
            pcl_color = pcl_color[choices]

        # --------------------------- FEAT used for SOFTGROUP -----------------------------
        # 对数据做augmentation，xyz_middle为augment之后坐标，xyz为平移+放大xyz_middle之后的坐标（用于voxelization)
        if self.augment:
            data = self.transform_train(point_cloud[:, 0:3], point_cloud[:, 3:6], semantic_labels, instance_labels, 1)
        else:
            data = self.transform_test(point_cloud[:, 0:3], point_cloud[:, 3:6], semantic_labels, instance_labels)
        xyz, xyz_middle, rgb, semantic_labels, instance_labels = data
        point_cloud = np.concatenate((xyz_middle, rgb), axis=1)

        # 得到场景内instance总数，每个instance中点的数量，instance所属label，点偏移量
        info = self.getInstanceInfo(xyz_middle, instance_labels, semantic_labels)
        inst_num, inst_pointnum, inst_cls, pt_offset_label = info

        # ------------------------------- LABELS ------------------------------
        target_bboxes = np.zeros((MAX_NUM_OBJ, 6))  # 场景内所有bbox的坐标+尺寸
        target_bboxes_mask = np.zeros((MAX_NUM_OBJ))  # 场景内的所有bbox的mask
        size_classes = np.zeros((MAX_NUM_OBJ,))
        size_residuals = np.zeros((MAX_NUM_OBJ, 3))
        num_bbox = 0  # 场景内bbox数量

        ref_box_label = np.zeros(MAX_NUM_OBJ)  # bbox label for reference target
        ref_center_label = np.zeros(3)  # bbox center for reference target
        ref_size_class_label = 0
        ref_size_residual_label = np.zeros(3)  # bbox size residual for reference target
        ref_box_corner_label = np.zeros((8, 3))
        ref_size_label = np.zeros(3)

        num_bbox = instance_bboxes.shape[0] if instance_bboxes.shape[0] < MAX_NUM_OBJ else MAX_NUM_OBJ
        target_bboxes_mask[0:num_bbox] = 1
        target_bboxes[0:num_bbox, :] = instance_bboxes[:MAX_NUM_OBJ, 0:6]

        # NOTE: set size class as semantic class. Consider use size2class.
        # 将instance_bbox的nyu40id映射到semantic class（0-19）
        class_ind = np.asarray([semantic_map[int(x)] for x in instance_bboxes[:num_bbox, -2]])
        size_classes[0:num_bbox] = class_ind
        size_residuals[0:num_bbox, :] = target_bboxes[0:num_bbox, 3:6] - DC.mean_size_arr[class_ind - 2,
                                                                         :]  # 和这个类平均尺寸的偏差
        # 上面不会出错，虽然semantic_map会将ceiling映射到-100,但预处理中已经去除了instance_bbox中属于这类的bbox
        # 所以DC.mean_size_arr不会出现indexError，减2是因为不考虑wall和floor这两类

        # construct the reference target label for each bbox
        for i, gt_id in enumerate(instance_bboxes[:num_bbox, -1]):
            if gt_id == object_id:
                ref_box_label[i] = 1
                ref_center_label = target_bboxes[i, 0:3]
                ref_size_label = target_bboxes[i, 3:6]
                ref_size_class_label = size_classes[i]
                ref_size_residual_label = size_residuals[i]

                # construct ground truth box corner coordinates
                ref_obb = DC.param2obb(ref_center_label, ref_size_class_label, ref_size_residual_label)
                ref_box_corner_label = get_3d_box(ref_obb[3:6], 0, ref_obb[0:3])

        # construct all GT bbox corners
        all_obb = DC.param2obb_batch(target_bboxes[:num_bbox, 0:3], size_classes[:num_bbox].astype(np.int64),
                                     size_residuals[:num_bbox])
        all_box_corner_label = get_3d_box_batch(all_obb[:, 3:6], np.zeros(num_bbox), all_obb[:, 0:3])

        # store
        gt_box_corner_label = np.zeros((MAX_NUM_OBJ, 8, 3))
        gt_box_masks = np.zeros((MAX_NUM_OBJ,))
        gt_box_object_ids = np.zeros((MAX_NUM_OBJ,))

        gt_box_corner_label[:num_bbox] = all_box_corner_label
        gt_box_masks[:num_bbox] = 1
        gt_box_object_ids[:num_bbox] = instance_bboxes[:, -1]

        target_bboxes_semcls = np.zeros((MAX_NUM_OBJ))
        target_object_ids = np.zeros((MAX_NUM_OBJ,))  # object ids of all objects
        try:
            target_bboxes_semcls[0:num_bbox] = [semantic_map[int(x)] for x in instance_bboxes[:, -2][0:num_bbox]]
            target_object_ids[0:num_bbox] = instance_bboxes[:, -1][0:num_bbox]
        except KeyError:
            pass

        object_cat = self.raw2label[object_name] if object_name in self.raw2label else 17

        data_dict = {}
        # dataset相关
        # ----------------------------------------------------------------------
        data_dict["dataset_idx"] = np.array(idx).astype(np.int64)  # 表示这是dataset中第几个sample

        # softgroup相关参数
        # ----------------------------------------------------------------------
        data_dict["scan_id"] = scene_id
        data_dict["coord"] = torch.from_numpy(xyz).long()  # scale过后的点坐标信息
        data_dict["coord_float"] = torch.from_numpy(xyz_middle)  # scale之前的原始点坐标信息
        data_dict["feat"] = torch.from_numpy(rgb).float()  # 点特征信息，这里是rgb color
        data_dict["semantic_label"] = torch.from_numpy(semantic_labels)
        data_dict["instance_label"] = torch.from_numpy(np.array(instance_labels).astype(np.int64))
        data_dict["inst_num"] = np.array(inst_num).astype(np.int64)
        data_dict["inst_pointnum"] = np.array(inst_pointnum).astype(np.int64)
        data_dict["inst_cls"] = np.array(inst_cls).astype(np.int64)
        data_dict["pt_offset_label"] = torch.from_numpy(pt_offset_label)

        # point-cloud data相关
        # ----------------------------------------------------------------------
        data_dict["point_clouds"] = point_cloud.astype(np.float32)  # point cloud data including features
        data_dict["pcl_color"] = pcl_color
        data_dict["semantic_label_nyu40id"] = np.array(semantic_labels_nyu40id).astype(np.int64)
        data_dict["object_id"] = torch.from_numpy(np.array(int(object_id)).astype(np.int64))  # 该物体在场景中的id
        data_dict["object_cat"] = np.array(object_cat).astype(np.int64)  # 该物体对应的category（0-17）
        data_dict["ann_id"] = np.array(int(ann_id)).astype(np.int64)  # 该描述的id

        # language description相关
        # ----------------------------------------------------------------------
        data_dict["lang_feat"] = torch.from_numpy(lang_feat.astype(np.float32))  # language feature vectors
        data_dict["lang_len"] = torch.from_numpy(np.array(lang_len).astype(np.int64))  # length of each description
        data_dict["lang_ids"] = torch.from_numpy(
            np.array(self.lang_ids[scene_id][str(object_id)][ann_id]).astype(np.int64))  # 对应单词idx的列表

        # GT bounding box相关，即该train sample对应的场景scene中的所有bbox
        # ----------------------------------------------------------------------
        data_dict["num_bbox"] = np.array(num_bbox).astype(np.int64)  # 该scene中共有多少instances（bounding box）
        data_dict["box_label_mask"] = target_bboxes_mask.astype(
            np.float32)  # (MAX_NUM_OBJ) as 0/1 with 1 indicating a unique box,1表示有bbox，0表示无
        data_dict["center_label"] = torch.from_numpy(target_bboxes.astype(np.float32)[:,
                                                     0:3])  # (MAX_NUM_OBJ, 3) for GT box center XYZ，即所有gt box的中心坐标
        data_dict["size_class_label"] = size_classes.astype(
            np.int64)  # (MAX_NUM_OBJ,) with int values in 0,...,NUM_SIZE_CLUSTER，表示每个object对应的class(0-17)
        data_dict["size_residual_label"] = size_residuals.astype(np.float32)  # (MAX_NUM_OBJ, 3) 每个object的尺寸

        # GT bounding box corner相关，即该train sample中对应的物体的bbox的corners坐标
        # ----------------------------------------------------------------------
        data_dict["gt_box_corner_label"] = torch.from_numpy(gt_box_corner_label.astype(
            np.float64))  # (MAX_NUM_OBJ，8，3) 所有 GT box的corners，NOTE type must be double
        data_dict["gt_box_masks"] = gt_box_masks.astype(np.int64)  # (MAX_NUM_OBJ)，1表示有bbox，0表示无
        data_dict["gt_box_object_ids"] = gt_box_object_ids.astype(np.int64)  # 各GT bbox对应的object ids

        # ref bounding box相关，即该train sample中对应的物体的bbox
        # ----------------------------------------------------------------------
        data_dict["ref_box_label"] = ref_box_label.astype(np.int64)  # 0/1 reference labels for each object bbox，1表示当前物体
        data_dict["ref_center_label"] = torch.from_numpy(ref_center_label.astype(np.float32))  # 该ref bbox的中心点坐标
        data_dict["ref_size_class_label"] = np.array(int(ref_size_class_label)).astype(
            np.int64)  # 该ref bbox对应的class（0-17）
        data_dict["ref_size_residual_label"] = ref_size_residual_label.astype(np.float32)  # 该ref bbox的尺寸误差数据
        data_dict["ref_box_corner_label"] = ref_box_corner_label.astype(
            np.float64)  # target box corners NOTE type must be double，即ref bbox的8个corner点坐标
        data_dict['ref_size_label'] = torch.from_numpy(ref_size_label.astype(np.float32))  # 该ref bbox的尺寸数据

        # target相关
        # ----------------------------------------------------------------------
        data_dict["sem_cls_label"] = target_bboxes_semcls.astype(np.int64)  # (MAX_NUM_OBJ,)，object对应的semantic class
        data_dict["scene_object_ids"] = torch.from_numpy(
            target_object_ids.astype(np.int64))  # (MAX_NUM_OBJ,)，object对应的object_id

        # unique_multiple，0表示该物体类型在场景中只有一个，1则表示该场景中有多个该种object
        data_dict["unique_multiple"] = np.array(self.unique_multiple_lookup[scene_id][str(object_id)][ann_id]).astype(
            np.int64)

        # 加载时间相关
        data_dict["load_time"] = time.time() - start

        return data_dict

    def collate_fn(self, batch):
        scan_ids = []
        coords = []
        coords_float = []
        feats = []
        semantic_labels = []
        instance_labels = []
        instance_pointnum = []  # (total_nInst), int
        instance_cls = []  # (total_nInst), long
        pt_offset_labels = []

        total_inst_num = 0
        batch_id = 0

        for data in batch:
            if data is None:
                continue
            # get data from dataset
            scan_id = data["scan_id"]
            coord = data["coord"]
            coord_float = data["coord_float"]
            feat = data["feat"]
            semantic_label = data["semantic_label"]
            instance_label = data["instance_label"]
            inst_num = data["inst_num"]
            inst_pointnum = data["inst_pointnum"]
            inst_cls = data["inst_cls"]
            pt_offset_label = data["pt_offset_label"]

            # append
            scan_ids.append(scan_id)
            coords.append(torch.cat([coord.new_full((coord.size(0), 1), batch_id), coord], 1))
            coords_float.append(coord_float)
            feats.append(feat)
            semantic_labels.append(semantic_label)
            instance_labels.append(instance_label)
            instance_pointnum.extend(inst_pointnum)
            instance_cls.extend(inst_cls)
            pt_offset_labels.append(pt_offset_label)
            batch_id += 1

        assert batch_id > 0, 'empty batch'
        if batch_id < len(batch):
            print(f'batch is truncated from size {len(batch)} to {batch_id}')

        # merge all the scenes in the batch
        coords = torch.cat(coords, 0)  # long (N, 1 + 3), the batch item idx is put in coords[:, 0]
        batch_idxs = coords[:, 0].int()
        coords_float = torch.cat(coords_float, 0).to(torch.float32)  # float (N, 3)
        feats = torch.cat(feats, 0)  # float (N, C)
        semantic_labels = torch.cat(semantic_labels, 0).long()  # long (N)
        instance_labels = torch.cat(instance_labels, 0).long()  # long (N)
        instance_pointnum = torch.tensor(instance_pointnum, dtype=torch.int)  # int (total_nInst)
        instance_cls = torch.tensor(instance_cls, dtype=torch.long)  # long (total_nInst)
        pt_offset_labels = torch.cat(pt_offset_labels).float()

        object_id = torch.cat([batch[i]['object_id'].unsqueeze(0) for i in range(len(batch))], 0)
        lang_feat = torch.cat([batch[i]['lang_feat'].unsqueeze(0) for i in range(len(batch))], 0)
        lang_len = torch.cat([batch[i]['lang_len'].unsqueeze(0) for i in range(len(batch))], 0)
        lang_ids = torch.cat([batch[i]['lang_ids'].unsqueeze(0) for i in range(len(batch))], 0)
        ref_size_label = torch.cat([batch[i]['ref_size_label'].unsqueeze(0) for i in range(len(batch))], 0)
        ref_center_label = torch.cat([batch[i]['ref_center_label'].unsqueeze(0) for i in range(len(batch))], 0)
        center_label = torch.cat([batch[i]['center_label'].unsqueeze(0) for i in range(len(batch))], 0)
        scene_object_ids = torch.cat([batch[i]['scene_object_ids'].unsqueeze(0) for i in range(len(batch))], 0)
        gt_box_corner_label = torch.cat([batch[i]['gt_box_corner_label'].unsqueeze(0) for i in range(len(batch))], 0)

        spatial_shape = np.clip(coords.max(0)[0][1:].numpy() + 1, self.voxel_cfg.spatial_shape[0], None)

        voxel_coords, v2p_map, p2v_map = voxelization_idx(coords, batch_id)

        return {
            # softgroup need
            'scan_ids': scan_ids,
            'coords': coords,
            'batch_idxs': batch_idxs,
            'voxel_coords': voxel_coords,
            'p2v_map': p2v_map,
            'v2p_map': v2p_map,
            'coords_float': coords_float,
            'feats': feats,
            'semantic_labels': semantic_labels,
            'instance_labels': instance_labels,
            'instance_pointnum': instance_pointnum,
            'instance_cls': instance_cls,
            'pt_offset_labels': pt_offset_labels,
            'spatial_shape': spatial_shape,
            'batch_size': batch_id,

            # proposal module need
            'object_id': object_id,
            'ref_size_label': ref_size_label,
            'ref_center_label': ref_center_label,
            'center_label': center_label,
            'gt_box_corner_label': gt_box_corner_label,
            'scene_object_ids': scene_object_ids,

            # caption module need
            'lang_feat': lang_feat,
            'lang_len': lang_len,
            'lang_ids': lang_ids,

        }


class ScannetReferenceTestDataset():

    def __init__(self, scanrefer_all_scene,
                 num_points=40000,
                 use_height=False,
                 use_color=False,
                 use_normal=False,
                 use_multiview=False):

        self.scanrefer_all_scene = scanrefer_all_scene  # all scene_ids in scanrefer
        self.num_points = num_points
        self.use_color = use_color
        self.use_height = use_height
        self.use_normal = use_normal
        self.use_multiview = use_multiview

        # load data
        self.scene_data = self._load_data()
        self.glove = pickle.load(open(GLOVE_PICKLE, "rb"))
        self.vocabulary = json.load(open(SCANREFER_VOCAB))
        self.multiview_data = {}

    def __len__(self):
        return len(self.scanrefer_all_scene)

    def __getitem__(self, idx):
        start = time.time()

        scene_id = self.scanrefer_all_scene[idx]

        # get pc
        mesh_vertices = self.scene_data[scene_id]["mesh_vertices"]

        if not self.use_color:
            point_cloud = mesh_vertices[:, 0:3]  # do not use color for now
            pcl_color = mesh_vertices[:, 3:6]
        else:
            point_cloud = mesh_vertices[:, 0:6]
            point_cloud[:, 3:6] = (point_cloud[:, 3:6] - MEAN_COLOR_RGB) / 256.0
            pcl_color = point_cloud[:, 3:6]

        if self.use_normal:
            normals = mesh_vertices[:, 6:9]
            point_cloud = np.concatenate([point_cloud, normals], 1)

        if self.use_multiview:
            # load multiview database
            pid = mp.current_process().pid
            if pid not in self.multiview_data:
                self.multiview_data[pid] = h5py.File(MULTIVIEW_DATA, "r", libver="latest")

            multiview = self.multiview_data[pid][scene_id]
            point_cloud = np.concatenate([point_cloud, multiview], 1)

        if self.use_height:
            floor_height = np.percentile(point_cloud[:, 2], 0.99)
            height = point_cloud[:, 2] - floor_height
            point_cloud = np.concatenate([point_cloud, np.expand_dims(height, 1)], 1)

        point_cloud, choices = random_sampling(point_cloud, self.num_points, return_choices=True)

        data_dict = {}
        data_dict["point_clouds"] = point_cloud.astype(np.float32)  # point cloud data including features
        data_dict["dataset_idx"] = idx
        data_dict["lang_feat"] = self.glove["sos"].astype(np.float32)  # GloVE embedding for sos token
        data_dict["load_time"] = time.time() - start

        return data_dict

    def _load_data(self):
        scene_data = {}
        for scene_id in self.scanrefer_all_scene:
            scene_data[scene_id] = {}
            scene_data[scene_id]["mesh_vertices"] = np.load(
                os.path.join(CONF.PATH.SCANNET_DATA, scene_id) + "_aligned_vert.npy")  # axis-aligned

        return scene_data
