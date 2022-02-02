from argparse import ArgumentParser
import os
from PIL import Image
from random import shuffle
from tqdm import tqdm

from composer.datasets.webdataset import create_webdataset


def parse_args():
    args = ArgumentParser()
    args.add_argument('--in_root', type=str, required=True)
    args.add_argument('--out_root', type=str, required=True)
    args.add_argument('--train_shards', type=int, default=128)
    args.add_argument('--val_shards', type=int, default=16)
    args.add_argument('--tqdm', type=int, default=1)
    return args.parse_args()


def get_train(in_root, wnids):
    pairs = []
    for wnid_idx, wnid in tqdm(enumerate(wnids), leave=False):
        in_dir = os.path.join(in_root, 'train', wnid, 'images')
        for basename in os.listdir(in_dir):
            filename = os.path.join(in_dir, basename)
            pairs.append((filename, wnid_idx))
    shuffle(pairs)
    return pairs


def get_val(in_root, wnid2idx):
    pairs = []
    filename = os.path.join(in_root, 'val', 'val_annotations.txt')
    lines = open(filename).read().strip().split('\n')
    for line in tqdm(lines, leave=False):
        basename, wnid = line.split()[:2]
        filename = os.path.join(in_root, 'val', 'images', basename)
        wnid_idx = wnid2idx[wnid]
        pairs.append((filename, wnid_idx))
    shuffle(pairs)
    return pairs


def each_sample(pairs):
    for idx, (img_file, cls) in enumerate(pairs):
        img = Image.open(img_file)
        yield {
            '__key__': f'{idx:05d}',
            'jpg': img,
            'cls': cls,
        }


def main(args):
    '''
    Directory layout:

        tiny-imagenet-200/
            test/
                images/
                    (10k images)
            train/
                (200 wnids)/
                    (500 images per dir)
            val/
                images/
                    (10k images)
                val_annotations.txt  # 10k rows of (file, wnid, x, y, h, w)
            wnids.txt  # 200 rows of (wnid)
            words.txt  # 82115 rows of (wnid, wordnet category name)

        web_tinyimagenet200/
            train_{shard}.tar
            val_{shard}.tar
    '''
    filename = os.path.join(args.in_root, 'wnids.txt')
    wnids = open(filename).read().split()
    wnid2idx = dict(zip(wnids, range(len(wnids))))

    pairs = get_train(args.in_root, wnids)
    create_webdataset(each_sample(pairs), args.out_root, 'train', len(pairs), args.train_shards, args.tqdm)

    pairs = get_val(args.in_root, wnid2idx)
    create_webdataset(each_sample(pairs), args.out_root, 'val', len(pairs), args.val_shards, args.tqdm)


if __name__ == '__main__':
    main(parse_args())
