import argparse
import csv
import os.path
import pathlib
import pickle
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pprint import pprint

import numpy as np
import pandas as pd
from easydict import EasyDict as edict
from tqdm import tqdm

from voc_tools.constants import VOC_IMAGES
from voc_tools.reader import list_dir, from_file
from voc_tools.utils import Dataset, VOCDataset
import sqlite3

from langchain_openai_tools import OpenAITextLoader, OpenAICredentialManager, OpenAITextEmbeddingDB


def generate_class_id_pickle(dataset_path, classes):
    """
    Generate and save CLASS_IDS into a pickle file

    Args:
        dataset_path: VOC dataset path
        classes: a list of classes to add to dataset
    """
    dataset_path = pathlib.Path(dataset_path)
    assert os.path.exists(str(dataset_path))
    # categorical encoding of the classes
    unique = list(set(classes))
    class_id = list(map(lambda x: unique.index(x) + 1, classes))
    # save as pickle file
    pickle_class_info_path = str(dataset_path / "train" / "class_info.pickle")
    with open(pickle_class_info_path, 'wb') as fpp:
        pickle.dump(class_id, fpp)
        print("'{}' is created with {} entries".format(pickle_class_info_path, len(class_id)))


def get_embedding_model(fasttext_model_path=None, emb_dim=300):
    """
    Args:
        fasttext_model_path: Pretrained fasttext  model (*.bin) file path See: https://fasttext.cc/docs/en/crawl-vectors.html
        emb_dim: Final embedding dimension
    """
    import fasttext
    import fasttext.util
    model = None
    model_name = ""
    if os.path.isfile(fasttext_model_path):
        model_name = pathlib.Path(fasttext_model_path).name
        print("Loading fasttext model:{}...".format(fasttext_model_path), end="")
        model = fasttext.load_model(fasttext_model_path)
        print("Loaded")
        if emb_dim != model.get_dimension():
            fasttext.util.reduce_model(model, emb_dim)
    assert model is not None, "A fasttext  model has to be initialised"
    return model, model_name, emb_dim


def generate_text_embedding_pickle(dataset_path, embeddings, model_name, emb_dim, mode="train"):
    """
    Generate and save caption embedding into a pickle file.
    Args:
        dataset_path: VOC dataset path
        embeddings: Prepared caption embeddings
        model_name: Model name which is used to prepare the embeddings
        emb_dim: Final embedding dimension
        mode: Generate embeddings for training or testing
    """
    assert mode in ("train", "test"), mode
    dataset_path = pathlib.Path(dataset_path)
    assert os.path.exists(str(dataset_path))
    # save as pickle file
    pickle_path = str(dataset_path / mode / "embeddings_{}_{}D.pickle".format(model_name, emb_dim))
    with open(pickle_path, 'wb') as fpp:
        pickle.dump(embeddings, fpp)
        print("'{}' is created with {} entries".format(pickle_path, len(embeddings)))


def generate_filename_pickle(dataset_path, filenames):
    """
    Generate a list of 'filenames' per 'caption', then save it as pickle format.
    Note: This is not a unique filename list. If, for a certain image, we have multiple
    captions, then multiple entries with same 'filename' will be created.

    Args:
        dataset_path: the path to the dataset
    """
    dataset_path = pathlib.Path(dataset_path)
    assert os.path.exists(str(dataset_path))
    
    pickle_filenames_path = str(dataset_path / "train" / "filenames.pickle")
    
    # save as pickle file
    with open(pickle_filenames_path, 'wb') as fpp:
        pickle.dump(filenames, fpp)
        print("'{}' is created with {} entries".format(pickle_filenames_path, len(filenames)))


STOP_WORDS = ['i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you', "you're", "you've", "you'll", "you'd",
              'your', 'yours', 'yourself', 'yourselves', 'he', 'him', 'his', 'himself', 'she', "she's", 'her', 'hers',
              'herself', 'it', "it's", 'its', 'itself', 'they', 'them', 'their', 'theirs', 'themselves', 'what',
              'which', 'who', 'whom', 'this', 'that', "that'll", 'these', 'those', 'am', 'is', 'are', 'was', 'were',
              'be', 'been', 'being', 'have', 'has', 'had', 'having', 'do', 'does', 'did', 'doing', 'a', 'an', 'the',
              'and', 'but', 'if', 'or', 'because', 'as', 'until', 'while', 'of', 'at', 'by', 'for', 'with', 'about',
              'against', 'between', 'into', 'through', 'during', 'before', 'after', 'above', 'below', 'to', 'from',
              'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under', 'again', 'further', 'then', 'once', 'here',
              'there', 'when', 'where', 'why', 'how', 'all', 'any', 'both', 'each', 'few', 'more', 'most', 'other',
              'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'can',
              'will', 'just', 'don', "don't", 'should', "should've", 'now', 'd', 'll', 'm', 'o', 're', 've', 'y', 'ain',
              'aren', "aren't", 'couldn', "couldn't", 'didn', "didn't", 'doesn', "doesn't", 'hadn', "hadn't", 'hasn',
              "hasn't", 'haven', "haven't", 'isn', "isn't", 'ma', 'mightn', "mightn't", 'mustn', "mustn't", 'needn',
              "needn't", 'shan', "shan't", 'shouldn', "shouldn't", 'wasn', "wasn't", 'weren', "weren't", 'won', "won't",
              'wouldn', "wouldn't"]


class DatasetWrap:
    def __init__(self, dataset_path, bulk=False, class_ids=False, fasttext_model_path=None,
                 embedding_dimension=300, test_captions=None) -> None:
        dataset_path = pathlib.Path(dataset_path)
        assert os.path.exists(str(dataset_path))
        self.dataset_path = pathlib.Path(dataset_path)
        self.is_bulk = bulk
        self.class_ids = class_ids
        self.embedding_dim = embedding_dimension
        self.test_captions = test_captions
        if fasttext_model_path is not None:
            # loading embedding
            self.emb_model, self.emb_model_name, _ = get_embedding_model(fasttext_model_path, embedding_dimension)
        else:
            self.emb_model, self.emb_model_name = None, None
        self.voc_data = VOCDataset(dataset_path, caption_support=True)
    
    def _prepare_classes(self):
        """
            Each image typically contains one or many captions. In two ways we can save the 'class_ids'
        into the pickle file, 'class_ids' per caption and 'class_ids' per image. 'bulk' parameter
        controls this action.
        """
        dataset_path = self.dataset_path
        if self.is_bulk:
            # 'class_ids per image'
            self.classes = [annotations[0].class_name for annotations, jpeg in self.voc_data.train.fetch(bulk=True)]
        else:
            # 'class_ids per caption'
            self.classes = list(
                map(lambda caption:
                    list(from_file(str(dataset_path / "train" / Dataset.CAPTION_DIR / caption.filename)))[
                        0].class_name,
                    self.voc_data.train.caption.fetch(bulk=False)))
        print("Class_id is prepared")
    
    def _prepare_filenames(self):
        """
            Each image typically contains one or many captions. In two ways we can save the filenames
        into the pickle file, filename per caption and filename per image. 'bulk' parameter
        controls this action.
        """
        dataset_path = self.dataset_path
        
        if self.is_bulk:
            
            mylist = [os.path.basename(file) for file in list_dir(str(dataset_path / "train"), dir_flag=VOC_IMAGES)]
        else:
            # read the filenames from captions
            mylist = [caption.filename.replace(".txt", ".jpg") for caption in
                      self.voc_data.train.caption.fetch(bulk=False)]
        self.filenames = list(map(lambda x: os.path.join("train", Dataset.IMAGE_DIR, x), mylist))  # fill path names
        print("Filenames is prepared")
    
    @staticmethod
    def clean(text):
        import re
        # remove punctuation, space, linefeed etc.
        text = text.strip().lower()
        text = " ".join(filter(lambda word: word not in STOP_WORDS, text.split(" ")))
        text = re.sub(r'[^\w\s\']', ' ', text)
        text = re.sub(r'[\s\n]+', ' ', text)
        return text.strip()
    
    def create_fasttext_data(self, text):
        with open("./temp_text_data.txt", "w", encoding="utf-8") as fp:
            fp.write(self.clean(text))
        return "./temp_text_data.txt"
    
    def train_fasttext_model(self, caption_data, fasttext_cfg):
        assert fasttext_cfg, "'fasttext_cfg' is required to train a fasttext model"
        keys = ("epoch", "lr", "algorithm")
        assert all(map(lambda x: x in keys,
                       fasttext_cfg.keys())), "The following keys are required:{} in 'fasttext_cfg'".format(keys)
        # train embedding model
        import fasttext
        data_path = self.create_fasttext_data(caption_data)
        model = fasttext.train_unsupervised(data_path, fasttext_cfg.algorithm, dim=self.embedding_dim, thread=4,
                                            epoch=fasttext_cfg.epoch, lr=fasttext_cfg.lr)
        self.emb_model = model
        self.emb_model_name = "fasttext_{}_{}".format(fasttext_cfg.algorithm, self.embedding_dim)
        # remove temporary files
        os.remove(data_path)
        print("Fasttext model is trained")
    
    def _prepare_embeddings(self, model, fasttext_cfg=None):
        """
            Each image typically contains one or many captions. In two ways we can save the caption
        embeddings into the pickle file. In as single instance or in bulk. Let, say we have 3 captions per image, and we
        have 2 images. If we want to store the embeddings as bulk, then it should look like:
        [[emb11, emb12, emb13], [emb21, emb22, emb23]]. If we want to store the embeddings as single instance, then it will be:
        [[emb11], [emb12], [emb13], [emb21], [emb22], [emb23]].
        """
        dataset_path = self.dataset_path
        mydata = VOCDataset(dataset_path, caption_support=True)
        if model is None:
            # get caption data
            caption_data = "".join(
                set(list(map(lambda caption: caption.captions.strip().strip(".").strip(),
                             mydata.train.caption.fetch(bulk=False)))))
            # train model
            self.train_fasttext_model(caption_data, fasttext_cfg)
            model = self.emb_model
        
        if self.is_bulk:
            self.embeddings = [list(map(lambda cap: model.get_word_vector(cap.captions), caption_list)) for caption_list
                               in mydata.train.caption.fetch(bulk=True)]
        else:
            self.embeddings = [[model.get_word_vector(caption.captions)] for caption in
                               mydata.train.caption.fetch(bulk=False)]
        print("Text embeddings is prepared for training")
        if self.test_captions is not None:
            self.test_embeddings = list(map(lambda cap: model.get_word_vector(cap), self.test_captions))
            print("Text embeddings is prepared for testing")
    
    def prepare_dataset(self, fasttext_cfg=None):
        if self.class_ids:
            self._prepare_classes()
        self._prepare_filenames()
        self._prepare_embeddings(self.emb_model, fasttext_cfg)
        generate_filename_pickle(str(self.dataset_path), self.filenames)
        if self.class_ids:
            generate_class_id_pickle(str(self.dataset_path), self.classes)
        generate_text_embedding_pickle(str(self.dataset_path), self.embeddings, self.emb_model_name, self.embedding_dim)
        if self.test_captions is not None:
            generate_text_embedding_pickle(str(self.dataset_path), self.test_embeddings, self.emb_model_name,
                                           self.embedding_dim, mode="test")


def parse_args():
    parser = argparse.ArgumentParser(description='Train a GAN network')
    parser.add_argument('--data_dir', dest='data_dir', help='Path to dataset directory.',
                        default='my_data', type=str, )
    parser.add_argument('--test_data_file', dest='test_data_file', help='A text file contains unseen captions to test',
                        default='', type=str, )
    parser.add_argument('--bulk', dest='bulk', default=False, action="store_true")
    parser.add_argument('--class_id', dest='class_id', default=False, action="store_true")
    parser.add_argument('--fasttext_train_lr', dest='fasttext_train_lr', type=float, default=None)
    parser.add_argument('--fasttext_train_algo', dest='fasttext_train_algo', type=str, default=None,
                        choices=['skipgram', 'cbow'])
    parser.add_argument('--fasttext_train_epoch', dest='fasttext_train_epoch', type=int, default=None)
    parser.add_argument('--fasttext_model', dest='fasttext_model', type=str, default=None)
    parser.add_argument('--emb_dim', dest='emb_dim', type=int, default=300)
    parser.add_argument('--sqlite', dest='sqlite',
                        help='Path to SQLite3 database file.',
                        default='', type=str, )
    parser.add_argument('--clean', dest='clean', default=False, action="store_true",
                        help="Clean before generating new data while using sqlite")
    parser.add_argument('--copy_images', dest='copy_images', default=False, action="store_true",
                        help="Copy images while generating new data using sqlite, --dataroot option is required")
    parser.add_argument('--dataroot', dest="dataroot", type=str,
                        help="This is a path to a image dataset to copy images while creating dataset using sqlite."
                             "The dataset format is default to PascalVOC format.")
    _args = parser.parse_args()
    pprint(_args)
    return _args


def generate_dataset(args):
    # creating object to generate compatible pickle files for StackGAN
    # prepare fasttext training configuration
    fasttext_cfg = edict()
    fasttext_cfg.epoch = args.fasttext_train_epoch
    fasttext_cfg.lr = args.fasttext_train_lr
    fasttext_cfg.algorithm = args.fasttext_train_algo
    # read the test captions
    test_captions = None
    if os.path.isfile(args.test_data_file):
        with open(args.test_data_file) as fp:
            test_captions = fp.readlines()
    # initialize dataset
    vdw = DatasetWrap(args.data_dir, args.bulk, args.class_id,
                      fasttext_model_path=args.fasttext_model,
                      embedding_dimension=args.emb_dim, test_captions=test_captions)
    vdw.prepare_dataset(fasttext_cfg)


def generate_openai_embeddings(embedding_database):
    from langchain.embeddings import OpenAIEmbeddings
    from openai.error import RateLimitError
    
    args = parse_args()
    
    class Data:
        total_sentence = 0
        total_tokens = 0
    
    emb_db = OpenAITextEmbeddingDB(embedding_database)
    print("Items in DB: {}".format(len(emb_db.db)))
    
    # emb_db.query(DatasetWrap.clean("A quick brown fox jump over the lazy dog"))
    
    def caption_loader():
        sqlite = SQLiteDataWrap(args.sqlite)
        unique_captions = sqlite.dataframe['caption'].apply(DatasetWrap.clean).unique()
        unique_captions = np.array(list(filter(lambda txt: not emb_db.is_available(txt), unique_captions)))
        print("Unique captions cleaned:", unique_captions.shape)
        Data.total_sentence = unique_captions.shape[0]
        Data.total_tokens = sum(map(lambda x: len(x), unique_captions))
        print("Total tokens:", Data.total_tokens)
        return unique_captions
    
    bulk_embedded = False
    rpm = 3
    bulk_caption_loader = OpenAITextLoader(caption_loader(), Data.total_tokens, Data.total_sentence, rpm=rpm,
                                           tpm=150000, auto_sleep=False)
    cred_man = OpenAICredentialManager("./data/openai.apikey")
    cm = iter(cred_man)
    key, nickname = next(cm)
    for caption in tqdm(caption_loader()):
        while True:
            model = OpenAIEmbeddings(openai_api_key=key, model="ada", max_retries=1)
            try:
                if cred_man.is_limit_exhausted(nickname):
                    raise RateLimitError("Rate limit exhausted for {}".format(nickname))
                
                # single
                embedding = model.embed_query(caption)
                emb_db.append(caption, embedding)
                emb_db.commit()
                # time.sleep(60 / rpm)
                break
            except RateLimitError:
                cred_man.set_limit_exhausted(nickname)
                key, nickname = next(cm)


def from_custom_dataset():
    args = parse_args()
    Dataset.IMAGE_DIR = "images"
    Dataset.ANNO_DIR = "Annotations"
    Dataset.CAPTION_DIR = "texts"
    # generate dataset
    generate_dataset(args)


class Caption:
    def __init__(self, items):
        self.idx = items[0]
        self.file_id = items[1]
        self.filename = self.file_id.replace(".jpg", ".txt")
        self.caption = items[2]
        self.author = items[3]
        self.is_occluded = items[4]
        self.is_error = items[5]


def check_grammar(text):
    # Create a LanguageTool instance
    try:
        from language_tool_python import LanguageTool
        tool = LanguageTool('en-US')
        
        # Check grammar in the text
        matches = tool.check(text)
        return matches
    except:
        return []


class SQLiteDataWrap:
    
    def __init__(self, dbpath):
        assert os.path.isfile(dbpath), dbpath
        conn = sqlite3.connect(dbpath)
        self.conn = conn
        self.dbpath = dbpath
        self.is_clean = False
        self.image_path_dict = None
        self.dataframe = pd.read_sql_query("SELECT * FROM caption", conn)
    
    def clean(self):
        self.is_clean = True
        return self
    
    def get_path(self, file_id, image_paths):
        if self.image_path_dict is None:
            self.image_path_dict = defaultdict(lambda: None)
        filepath = self.image_path_dict[file_id]
        if filepath is None:
            filtered = list(filter(lambda fname: file_id in fname, image_paths))
            if len(filtered) > 0:
                filepath = filtered[0]
                self.image_path_dict[file_id] = filepath
        return filepath
    
    def export(self, data_root, clean=False, copy_images=False, image_paths=()):
        data_root = pathlib.Path(data_root)
        
        # defining paths
        caption_root = data_root / "train" / "texts"
        image_root = data_root / "train" / "JPEGImages"
        if clean:
            print("Cleaning previous data...", end="")
            # deleting directories
            shutil.rmtree(caption_root, ignore_errors=True)
            shutil.rmtree(image_root, ignore_errors=True)
            print("Done")
        # create directories
        os.makedirs(caption_root, exist_ok=True)
        os.makedirs(image_root, exist_ok=True)
        os.makedirs(data_root / "test" / "JPEGImages", exist_ok=True)
        os.makedirs(data_root / "test" / "JPEGImages", exist_ok=True)
        
        statistics = defaultdict(lambda: defaultdict(lambda: 0))
        # taking DB cursor and running queries
        print("Quering dataset form SQLITE...")
        curr = self.conn.cursor()
        count = curr.execute("SELECT count(*) FROM caption").fetchone()[0]
        dataset = curr.execute("SELECT * FROM caption")
        print("Creating dataset form SQLITE...", )
        for idx, data in enumerate(dataset):
            print("\r{}/{} {}% ".format(idx, (count - 1), round((idx / (count - 1) * 100.), 2)), end="\b")
            caption = Caption(data)
            if copy_images:
                # copying images
                filepath = self.get_path(caption.file_id, image_paths)
                if not os.path.isfile(image_root / caption.file_id):
                    shutil.copyfile(filepath, image_root / caption.file_id)
            
            # creating the caption files
            with open(caption_root / caption.filename, "a+") as fp:
                fp.seek(0)
                lines = fp.readlines()
                if caption.caption + "\n" in lines:
                    statistics[caption.filename]['duplicate'] += 1
                    statistics["caption"]['duplicate'] += 1
                elif len(caption.caption.split(" ")) < 2:
                    statistics[caption.filename]['faulty'] += 1
                    statistics["caption"]['faulty'] += 1
                else:
                    fp.write(caption.caption + "\n")
                    # check grammar
                    errors = check_grammar(caption.caption)
                    # save statistics
                    statistics[caption.filename]['caption'] += 1
                    statistics[caption.filename]['grammar'] += len(errors) > 0
                    statistics["caption"]['total'] += 1
                    statistics['grammar']['error-sentence'] += len(errors) > 0
                    statistics['grammar']['total-error'] += len(errors)
        print("Done")
        return statistics


def from_sqlite():
    args = parse_args()
    dataset = pathlib.Path(args.dataroot)
    # For reading images
    Dataset.IMAGE_DIR = "JPEGImages"
    # reading filepaths
    file_paths = list(list_dir(str(dataset / "train"), dir_flag=VOC_IMAGES, fullpath=True))
    file_paths.extend(list(list_dir(str(dataset / "test"), dir_flag=VOC_IMAGES, fullpath=True)))
    # generating dataset form SQLIte
    sqlite_data = SQLiteDataWrap(args.sqlite)
    sqlite_data.clean().export(args.data_dir, clean=args.clean, copy_images=args.copy_images, image_paths=file_paths)
    # For generating dataset
    Dataset.IMAGE_DIR = "JPEGImages"
    Dataset.CAPTION_DIR = "texts"
    # generate dataset
    generate_dataset(args)


if __name__ == '__main__':
    # from_custom_dataset()
    # from_sqlite()
    for text in [
        "A quick brown fox jump over the lazy dog",
        "A sharp knife in a bag.",
        "Security discovered a concealed knife in the passenger's bag.",
    ]:
        print(DatasetWrap.clean(text))
    generate_openai_embeddings("./data/openai_embedding.db")

""" UBUNTU
sqlite3 -header -csv "C:\\Users\\dndlssardar\\Downloads\\tip_gai_22052023_1743.db" "SELECT * FROM caption" > caption.csv
python data/generate_custom_dataset.py --data_dir data/sixray_sample --emb_dim 300 --fasttext_model /mnt/c/Users/dndlssardar/Downloads/Fasttext/cc.en.300.bin
python data/generate_custom_dataset.py --data_dir data/sixray_500 --fasttext_model /data/fasttext/cc.en.300.bin  --sqlite /data/sixray_caption_db/<tip_gai.db> --clean --copy_images --dataroot /data/Sixray_easy/
python data/generate_custom_dataset.py --data_dir data/sixray_500 --fasttext_model /mnt/c/Users/dndlssardar/Downloads/Fasttext/cc.en.300.bin --sqlite data/tip_gai.db --clean --copy_images --dataroot "/mnt/c/Users/dndlssardar/OneDrive - Smiths Group/Documents/Projects/Dataset/Sixray_easy"
"""
