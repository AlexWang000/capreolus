import pickle
import os
import tensorflow as tf
import numpy as np
from collections import defaultdict
import random
from profane import ConfigOption
from profane.base import Dependency
from tqdm import tqdm

from capreolus.extractor import Extractor
from capreolus import get_logger
from capreolus.utils.common import padlist
from capreolus.utils.exceptions import MissingDocError

logger = get_logger(__name__)


@Extractor.register
class BertPassage(Extractor):
    """
    Extracts passages from the document to be later consumed by a BERT based model.
    Does NOT use all the passages. The first passages is always used. Use the `prob` config to control the probability
    of a passage being selected
    """
    module_name = "bertpassage"
    dependencies = [
        Dependency(
            key="index", module="index", name="anserini", default_config_overrides={"indexstops": True, "stemmer": "none"}
        ),
        Dependency(key="tokenizer", module="tokenizer", name="berttokenizer"),
    ]

    pad = 0
    pad_tok = "[PAD]"

    config_spec = [
        ConfigOption("maxseqlen", 256, "Maximum input length for BERT"),
        ConfigOption("usecache", False, "Should the extracted features be cached?"),
        ConfigOption("passagelen", 150, "Length of the extracted passage"),
        ConfigOption("stride", 100, "Stride"),
        ConfigOption("numpassages", 16, "Number of passages per document"),
        ConfigOption(
            "prob",
            0.1,
            "The probability that a passage from the document will be used for training (the first passage is always used)",
        ),
    ]

    def load_state(self, qids, docids):
        cache_fn = self.get_state_cache_file_path(qids, docids)
        with open(cache_fn, "rb") as f:
            state_dict = pickle.load(f)
            self.qid2toks = state_dict["qid2toks"]
            self.docid2passages = state_dict["docid2passages"]

    def cache_state(self, qids, docids):
        os.makedirs(self.get_cache_path(), exist_ok=True)
        with open(self.get_state_cache_file_path(qids, docids), "wb") as f:
            state_dict = {"qid2toks": self.qid2toks, "docid2passages": self.docid2passages}
            pickle.dump(state_dict, f, protocol=-1)

    def get_tf_feature_description(self):
        feature_description = {
            "posdoc": tf.io.FixedLenFeature([], tf.string),
            "posdoc_mask": tf.io.FixedLenFeature([], tf.string),
            "posdoc_seg": tf.io.FixedLenFeature([], tf.string),
            "negdoc": tf.io.FixedLenFeature([], tf.string),
            "negdoc_mask": tf.io.FixedLenFeature([], tf.string),
            "negdoc_seg": tf.io.FixedLenFeature([], tf.string),
            "label": tf.io.FixedLenFeature([], tf.string),
        }

        return feature_description

    def create_tf_train_feature(self, sample):
        """
        Returns a set of features from a doc.
        Of the num_passages passages that are present in a document, we use only a subset of it.
        params:
        sample - A dict where each entry has the shape [batch_size, num_passages, maxseqlen]

        Returns a list of features. Each feature is a dict, and each value in the dict has the shape [batch_size, maxseqlen].
        Yes, the output shape is different to the input shape because we sample from the passages.
        """
        num_passages = self.config["numpassages"]

        def _bytes_feature(value):
            """Returns a bytes_list from a string / byte. Our features are multi-dimensional tensors."""
            if isinstance(value, type(tf.constant(0))):  # if value ist tensor
                value = value.numpy()  # get value of tensor
            return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

        posdoc, negdoc, negdoc_id = sample["pos_bert_input"], sample["neg_bert_input"], sample["negdocid"]
        posdoc_mask, posdoc_seg, negdoc_mask, negdoc_seg = (
            sample["pos_mask"],
            sample["pos_seg"],
            sample["neg_mask"],
            sample["neg_seg"],
        )
        label = sample["label"]
        features = []

        for i in range(num_passages):
            # Always use the first passage, then sample from the remaining passages
            if i > 0 and random.random() > self.config["prob"]:
                continue

            bert_input_line = posdoc[i]
            bert_input_line = " ".join(self.tokenizer.bert_tokenizer.convert_ids_to_tokens(list(bert_input_line)))
            passage = bert_input_line.split("[SEP]")[-2]

            # Ignore empty passages as well
            if passage.strip() == "[PAD]":
                continue

            feature = {
                "pos_bert_input": _bytes_feature(tf.io.serialize_tensor(posdoc[i])),
                "pos_mask": _bytes_feature(tf.io.serialize_tensor(posdoc_mask[i])),
                "pos_seg": _bytes_feature(tf.io.serialize_tensor(posdoc_seg[i])),
                "neg_bert_input": _bytes_feature(tf.io.serialize_tensor(negdoc[i])),
                "neg_mask": _bytes_feature(tf.io.serialize_tensor(negdoc_mask[i])),
                "neg_seg": _bytes_feature(tf.io.serialize_tensor(negdoc_seg[i])),
                "label": _bytes_feature(tf.io.serialize_tensor(label[i])),
            }
            features.append(feature)

        return features

    def create_tf_dev_feature(self, sample):
        """
        Unlike the train feature, the dev set uses all passages. Both the input and the output are dicts with the shape
        [batch_size, num_passages, maxseqlen]
        """
        posdoc, negdoc, negdoc_id = sample["pos_bert_input"], sample["neg_bert_input"], sample["negdocid"]
        posdoc_mask, posdoc_seg, negdoc_mask, negdoc_seg = (
            sample["pos_mask"],
            sample["pos_seg"],
            sample["neg_mask"],
            sample["neg_seg"],
        )
        label = sample["label"]

        def _bytes_feature(value):
            """Returns a bytes_list from a string / byte."""
            if isinstance(value, type(tf.constant(0))):  # if value ist tensor
                value = value.numpy()  # get value of tensor
            return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

        feature = {
            "pos_bert_input": _bytes_feature(tf.io.serialize_tensor(posdoc)),
            "pos_mask": _bytes_feature(tf.io.serialize_tensor(posdoc_mask)),
            "pos_seg": _bytes_feature(tf.io.serialize_tensor(posdoc_seg)),
            "neg_bert_input": _bytes_feature(tf.io.serialize_tensor(negdoc)),
            "neg_mask": _bytes_feature(tf.io.serialize_tensor(negdoc_mask)),
            "neg_seg": _bytes_feature(tf.io.serialize_tensor(negdoc_seg)),
            "label": _bytes_feature(tf.io.serialize_tensor(label)),
        }

        return [feature]

    def parse_tf_train_example(self, example_proto):
        feature_description = self.get_tf_feature_description()
        parsed_example = tf.io.parse_example(example_proto, feature_description)

        def parse_tensor_as_int(x):
            parsed_tensor = tf.io.parse_tensor(x, tf.int64)
            parsed_tensor.set_shape([self.config["maxseqlen"]])

            return parsed_tensor

        def parse_label_tensor(x):
            parsed_tensor = tf.io.parse_tensor(x, tf.float32)
            parsed_tensor.set_shape([2])

            return parsed_tensor

        pos_bet_input = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc"], dtype=tf.int64)
        pos_mask = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc_mask"], dtype=tf.int64)
        pos_seg = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc_seg"], dtype=tf.int64)
        neg_bert_input = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc"], dtype=tf.int64)
        neg_mask = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc_mask"], dtype=tf.int64)
        neg_seg = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc_seg"], dtype=tf.int64)
        label = tf.map_fn(parse_label_tensor, parsed_example["label"], dtype=tf.float32)

        return (pos_bet_input, pos_mask, pos_seg, neg_bert_input, neg_mask, neg_seg), label

    def parse_tf_dev_example(self, example_proto):
        feature_description = self.get_tf_feature_description()
        parsed_example = tf.io.parse_example(example_proto, feature_description)

        def parse_tensor_as_int(x):
            parsed_tensor = tf.io.parse_tensor(x, tf.int64)
            parsed_tensor.set_shape([self.config["numpassages"], self.config["maxseqlen"]])

            return parsed_tensor

        def parse_label_tensor(x):
            parsed_tensor = tf.io.parse_tensor(x, tf.float32)
            parsed_tensor.set_shape([self.config["numpassages"], 2])

            return parsed_tensor

        pos_bert_input = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc"], dtype=tf.int64)
        pos_mask = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc_mask"], dtype=tf.int64)
        pos_seg = tf.map_fn(parse_tensor_as_int, parsed_example["posdoc_seg"], dtype=tf.int64)
        neg_bert_input = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc"], dtype=tf.int64)
        neg_mask = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc_mask"], dtype=tf.int64)
        neg_seg = tf.map_fn(parse_tensor_as_int, parsed_example["negdoc_seg"], dtype=tf.int64)
        label = tf.map_fn(parse_label_tensor, parsed_example["label"], dtype=tf.float32)

        return (pos_bert_input, pos_mask, pos_seg, neg_bert_input, neg_mask, neg_seg), label

    def get_passages_for_doc(self, doc):
        """
        Extract passages from the doc.
        If there are too many passages, keep the first and the last one and sample from the rest.
        If there are not enough packages, pad.
        """
        tokenize = self.tokenizer.tokenize
        numpassages = self.config["numpassages"]
        passages = []

        for i in range(0, len(doc), self.config["stride"]):
            if i >= len(doc):
                assert len(passages) > 0, f"no passage can be built from empty document {doc}"
                break
            else:
                passage = doc[i: i + self.config["passagelen"]]

            passages.append(tokenize(" ".join(passage)))

        n_actual_passages = len(passages)
        # If we have a more passages than required, keep the first and last, and sample from the rest
        if n_actual_passages > numpassages:
            passages = [passages[0]] + random.sample(passages[1:-1], numpassages - 2) + [passages[-1]]
        else:
            # Pad until we have the required number of passages
            for _ in range(numpassages - n_actual_passages):
                passages.append(["[PAD]"])

        assert len(passages) == self.config["numpassages"]
        return passages

    def _build_vocab(self, qids, docids, topics):
        if self.is_state_cached(qids, docids) and self.config["usecache"]:
            self.load_state(qids, docids)
            logger.info("Vocabulary loaded from cache")
        else:
            logger.info("Building bertpassage vocabulary")
            self.docid2passages = {}

            for docid in tqdm(docids, "extract passages"):
                # Naive tokenization based on white space
                doc = self.index.get_doc(docid).split()
                passages = self.get_passages_for_doc(doc)
                self.docid2passages[docid] = passages

            self.qid2toks = {qid: self.tokenizer.tokenize(topics[qid]) for qid in tqdm(qids, desc="querytoks")}
            self.cache_state(qids, docids)

    def exist(self):
        return hasattr(self, "docid2passages") and len(self.docid2passages)

    def preprocess(self, qids, docids, topics):
        if self.exist():
            return

        self.index.create_index()
        self.qid2toks = defaultdict(list)
        self.docid2passages = None

        self._build_vocab(qids, docids, topics)

    def id2vec(self, qid, posid, negid=None, label=None):
        """
        See parent class for docstring
        """
        assert label is not None

        tokenizer = self.tokenizer
        maxseqlen = self.config["maxseqlen"]

        query_toks = self.qid2toks[qid]
        pos_bert_inputs = []
        pos_bert_masks = []
        pos_bert_segs = []

        # N.B: The passages in self.docid2passages are not bert tokenized
        pos_passages = self.docid2passages[posid]
        for tokenized_passage in pos_passages:
            input_line = ["[CLS]"] + query_toks + ["[SEP]"] + tokenized_passage + ["[SEP]"]
            if len(input_line) > maxseqlen:
                input_line = input_line[:maxseqlen]
                input_line[-1] = "[SEP]"

            padded_input_line = padlist(input_line, padlen=self.config["maxseqlen"], pad_token=self.pad_tok)
            pos_bert_masks.append([1] * len(input_line) + [0] * (len(padded_input_line) - len(input_line)))
            pos_bert_segs.append([0] * (len(query_toks) + 2) + [1] * (len(padded_input_line) - len(query_toks) - 2))
            pos_bert_inputs.append(tokenizer.convert_tokens_to_ids(padded_input_line))

        data = {
            "posdocid": posid,
            "posdoc": np.array(pos_bert_inputs, dtype=np.long),
            "posdoc_mask": np.array(pos_bert_masks, dtype=np.long),
            "posdoc_seg": np.array(pos_bert_segs, dtype=np.long),
            "negdocid": "",
            "negdoc": np.zeros((self.config["numpassages"], self.config["maxseqlen"]), dtype=np.long),
            "negdoc_mask": np.zeros((self.config["numpassages"], self.config["maxseqlen"]), dtype=np.long),
            "negdoc_seg": np.zeros((self.config["numpassages"], self.config["maxseqlen"]), dtype=np.long),
            "label": np.repeat(np.array([label], dtype=np.float32), self.config["numpassages"], 0),
        }

        if negid:
            neg_bert_inputs = []
            neg_bert_masks = []
            neg_bert_segs = []
            neg_passages = self.docid2passages[negid]
            for tokenized_passage in neg_passages:
                input_line = ["[CLS]"] + query_toks + ["[SEP]"] + tokenized_passage + ["[SEP]"]
                if len(input_line) > maxseqlen:
                    input_line = input_line[:maxseqlen]
                    input_line[-1] = "[SEP]"

                padded_input_line = padlist(input_line, padlen=self.config["maxseqlen"], pad_token=self.pad_tok)
                neg_bert_masks.append([1] * len(input_line) + [0] * (len(padded_input_line) - len(input_line)))
                neg_bert_segs.append([0] * (len(query_toks) + 2) + [1] * (len(padded_input_line) - len(query_toks) - 2))
                neg_bert_inputs.append(tokenizer.convert_tokens_to_ids(padded_input_line))

            if not neg_bert_inputs:
                raise MissingDocError(qid, negid)

            data["negdocid"] = negid
            data["negdoc"] = np.array(neg_bert_inputs, dtype=np.long)
            data["negdoc_mask"] = np.array(neg_bert_masks, dtype=np.long)
            data["negdoc_seg"] = np.array(neg_bert_segs, dtype=np.long)

        return data
