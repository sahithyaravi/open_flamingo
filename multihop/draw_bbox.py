"""
Turn the merged data into some tfrecord files.
"""
import sys

sys.path.append('../../')
import argparse
import hashlib
import io
import json
import os
import random
import numpy as np
from tempfile import TemporaryDirectory
from copy import deepcopy

from PIL import Image, ImageDraw, ImageFont
import tensorflow as tf
from sacremoses import MosesDetokenizer
import regex as re
from tqdm import tqdm
import pandas as pd

from collections import defaultdict
import colorsys
import hashlib

detokenizer = MosesDetokenizer(lang='en')
import sys
sys.path.append('../')
import tensorflow as tf

from tempfile import TemporaryDirectory
import os
import argparse
from PIL import Image
import numpy as np
from io import BytesIO
import random


class GCSTFRecordWriter(object):
    def __init__(self, fn, auto_close=False, options=None):
        """
        Shuffle things in the shuffle buffer and write to tfrecords

        If buffer_size == 0 then no shuffling
        :param fn:
        :param buffer_size:
        """
        self.fn = fn
        if fn.startswith('gs://'):
            self.gclient = storage.Client()
            self.storage_dir = TemporaryDirectory()
            self.writer = tf.io.TFRecordWriter(os.path.join(self.storage_dir.name, 'temp.tfrecord'), options=options)
            self.bucket_name, self.file_name = self.fn.split('gs://', 1)[1].split('/', 1)

        else:
            self.gclient = None
            self.bucket_name = None
            self.file_name = None
            self.storage_dir = None
            self.writer = tf.io.TFRecordWriter(fn, options=options)
        self.auto_close=auto_close

    def write(self, x):
        self.writer.write(x)

    def close(self):
        self.writer.close()
        if self.gclient is not None:
            print("UPLOADING!!!!!", flush=True)
            bucket = self.gclient.get_bucket(self.bucket_name)
            blob = bucket.blob(self.file_name)
            blob.upload_from_filename(os.path.join(self.storage_dir.name, 'temp.tfrecord'))
            self.storage_dir.cleanup()

    def __enter__(self):
        # Called when entering "with" context.
        return self

    def __exit__(self, *_):
        # Called when exiting "with" context.
        # Upload shit
        if self.auto_close:
            print("CALLING CLOSE")
            self.close()


def int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))


def int64_list_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def bytes_list_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))


def float_list_feature(value):
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def get_size_for_resize(image_size, shorter_size_trg=384, longer_size_max=512):
    """
    Gets a new size for the image. We will try to make it such that the bigger size is less than
    longer_size_max. However, we won't resize it if its shortest side is <= shorter_size_trg.
    :param image_size:
    :param shorter_size_trg:
    :param longer_size_max:
    :return:
    """

    w, h = image_size
    size = shorter_size_trg  # Try [size, size]

    if min(w, h) <= size:
        return w, h

    min_original_size = float(min((w, h)))
    max_original_size = float(max((w, h)))
    if max_original_size / min_original_size * size > longer_size_max:
        size = int(round(longer_size_max * min_original_size / max_original_size))

    if (w <= h and w == size) or (h <= w and h == size):
        return w, h
    if w < h:
        ow = size
        oh = int(size * h / w)
    else:
        oh = size
        ow = int(size * w / h)
    return ow, oh

def resize_image(image, shorter_size_trg=384, longer_size_max=512):
    """
    Resize image such that the longer size is <= longer_size_max.
    Gets a new size for the image. We will try to make it such that the bigger size is less than
    longer_size_max. However, we won't resize it if its shortest side is <= shorter_size_trg.
    :param image:
    :param shorter_size_trg:
    :param longer_size_max:
    """
    trg_size = get_size_for_resize(image.size, shorter_size_trg=shorter_size_trg,
                                       longer_size_max=longer_size_max)
    if trg_size != image.size:
        return image.resize(trg_size, resample=Image.BICUBIC)
    return image

def pil_image_to_jpgstring(image: Image, quality=95):
    """
    :param image: PIL image
    :return: it, as a jpg string
    """
    with BytesIO() as output:
        image.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()


def create_base_parser():
    parser = argparse.ArgumentParser(description='SCRAPE!')
    parser.add_argument(
        '-fold',
        dest='fold',
        default=0,
        type=int,
        help='which fold we are on'
    )
    parser.add_argument(
        '-num_folds',
        dest='num_folds',
        default=1,
        type=int,
        help='Number of folds (corresponding to both the number of training files and the number of testing files)',
    )
    parser.add_argument(
        '-seed',
        dest='seed',
        default=1337,
        type=int,
        help='which seed to use'
    )
    parser.add_argument(
        '-split',
        dest='split',
        default='train',
        type=str,
        help='which split to use'
    )
    parser.add_argument(
        '-base_fn',
        dest='base_fn',
        default='gs://replace_with_your_path/',
        type=str,
        help='Base filename to use. You can start this with gs:// and we\'ll put it on google cloud.'
    )
    return parser

def draw_boxes_on_image(image, metadata, tokenl_to_names, flip_lr=False):
    """
    Draw boxes on the image
    :param image:
    :param metadata:
    :param tokenl_to_names:
    :return:
    """
    #####################################################
    # last draw boxes on images
    image_copy = deepcopy(image)
    scale_factor = image.size[0] / metadata['width']

    boxes_to_draw = sorted(set([z for x in tokenl_to_names.keys() for z in x]))
    font_i = ImageFont.truetype(font='/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf', size=17)

    for i in boxes_to_draw:
        name_i = tokenl_to_names[tuple([i])]
        # print(name_i)
        box_i = np.array(metadata['boxes'][i][:4]) * scale_factor
        color_hash = int(hashlib.sha256(name_i.encode('utf-8')).hexdigest(), 16)

        # Hue between [0,1],
        hue = (color_hash % 1024) / 1024
        sat = (color_hash % 1023) / 1023

        # luminosity around [0.5, 1.0] for border
        l_start = 0.4
        l_offset = ((color_hash % 1025) / 1025)
        lum = l_offset * (1.0 - l_start) + l_start
        txt_lum = l_offset * 0.1

        color_i = tuple((np.array(colorsys.hls_to_rgb(hue, lum, sat)) * 255.0).astype(np.int32).tolist())
        txt_colori = tuple((np.array(colorsys.hls_to_rgb(hue, txt_lum, sat)) * 255.0).astype(np.int32).tolist())

        x1, y1, x2, y2 = box_i.tolist()
        if flip_lr:
            x1_tmp = image_copy.width - x2
            x2 = image_copy.width - x1
            x1 = x1_tmp

        shape = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]

        draw = ImageDraw.Draw(image_copy, mode='RGBA')
       
        # draw.line(shape, fill=color_i, width=3)
        draw.rectangle([(x1, y1), (x2, y2)], fill=color_i + (32,), outline=color_i + (255,), width=2)
        draw.text((x1+1, y1+1), name_i,fill=color_i + (255,), 
                  font=font_i)
        txt_w, txt_h = font_i.getsize(name_i)

    return image_copy


def iterate_through_examples():
    split = "train"
    data_root = '/ubc/cs/research/nlp/sahiravi/LAVIS/data'
    images_path = f"{data_root}/vcr/vcr1images/"
    annots =  f'{data_root}/vcr/{split}.jsonl'

    with open(annots, 'r') as f:
        for idx, l in enumerate(f):

            item = json.loads(l)
            with open(os.path.join(images_path, item['metadata_fn']), 'r') as f:
                metadata = json.load(f)

            image = Image.open(os.path.join(images_path, item['img_fn']))
            image = resize_image(image, shorter_size_trg=450, longer_size_max=800)

            ######################################################################
            # Tie tokens with names
            # the metadata file has the names only, ie
            # ['person', 'person', 'person', 'car']

            # questions refer to this through an index, ie
            # 2 for person3
            tokenl_to_names = {}
            type_to_ids_globally = defaultdict(list)
            object_count_idx = []

            for obj_id, obj_type in enumerate(metadata['names']):
                object_count_idx.append(len(type_to_ids_globally[obj_type]))
                type_to_ids_globally[obj_type].append(obj_id)

            for obj_type, obj_ids in type_to_ids_globally.items():
                if len(obj_ids) == 1:
                    # Replace all mentions with that single object ('car') with "the X"
                    tokenl_to_names[tuple(obj_ids)] = f'the {obj_type}'

            def get_name_from_idx(k):
                """
                If k has a length of 1: we're done
                otherwise, recurse and join
                :param k: A tuple of indices
                :return:
                """
                if k in tokenl_to_names:
                    return tokenl_to_names[k]

                if len(k) == 1:
                    obj_type = metadata['names'][k[0]]
                    obj_idx = object_count_idx[k[0]]
                    name = '{} {}'.format(obj_type.capitalize(), obj_idx+1)
                    tokenl_to_names[k] = name
                    return name

                names = [get_name_from_idx(tuple([k_sub])) for k_sub in k]

                if len(names) <= 2:
                    names = ' and '.join(names)
                else:
                    # who gives a fuck about an oxford comma
                    names = ' '.join(names[:-2]) + ' ' + ' and '.join(names[-2:])

                tokenl_to_names[k] = names
                return names

            def fix_token(tok):
                """
                Fix token that's either a list (of object detections) or a word
                :param tok:
                :return:
                """
                if not isinstance(tok, list):
                    # just in case someone said `Answer:'. unlikely...
                    if tok != 'Answer:':
                        return tok.replace(':', ' ')
                    return tok
                return get_name_from_idx(tuple(tok)[:2])

            def fix_tokenl(token_list):
                out = detokenizer.detokenize([fix_token(tok) for tok in token_list])
                out = re.sub(" n't", "n't", out)
                out = re.sub("n' t", "n't", out)

                # remove space before some punctuation
                out = re.sub(r'\s([\',\.\?])', r'\1', out)
                # fix shit like this: `he' s writing.`"
                out = re.sub(r'\b\'\ss', "'s", out)

                # kill some punctuation
                out = re.sub(r'\-\;', ' ', out)

                # remove extra spaces
                out = re.sub(r'\s+', ' ', out.strip())
                return out

            qa_query = fix_tokenl(item['question'])
            qa_choices = [fix_tokenl(choice) for choice in item['answer_choices']]
            qar_choices = [fix_tokenl(choice) for choice in item['rationale_choices']]

            img_boxes = draw_boxes_on_image(image, metadata, tokenl_to_names)
            img_boxes.save("img1.png")
            break

iterate_through_examples()