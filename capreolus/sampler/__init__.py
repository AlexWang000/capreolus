from profane import import_all_modules

# import_all_modules(__file__, __package__)

import random
from itertools import product
import hashlib
import torch.utils.data

from profane import ModuleBase, Dependency, ConfigOption, constants
from capreolus.utils.exceptions import MissingDocError
from capreolus.utils.loginit import get_logger


logger = get_logger(__name__)


class Sampler(ModuleBase):
    module_type = "sampler"

    def prepare(self, qid_to_docids, qrels, extractor, relevance_level=1, **kwargs):
        """
        params:
        qid_to_docids: A dict of the form {qid: [list of docids to rank]}
        qrels: A dict of the form {qid: {docid: label}}
        extractor: An Extractor instance (eg: EmbedText)
        relevance_level: Threshold score below which documents are considered to be non-relevant.
        """
        self.extractor = extractor

        # remove qids from qid_to_docids that do not have relevance labels in the qrels
        self.qid_to_docids = {qid: docids for qid, docids in qid_to_docids.items() if qid in qrels}
        if len(self.qid_to_docids) != len(qid_to_docids):
            logger.warning("skipping qids that were missing from the qrels: {}".format(qid_to_docids.keys() - self.qid_to_docids.keys()))

        self.qid_to_reldocs = {
            qid: [docid for docid in docids if qrels[qid].get(docid, 0) >= relevance_level]
            for qid, docids in self.qid_to_docids.items()
        }
        # TODO option to include only negdocs in a top k
        self.qid_to_negdocs = {
            qid: [docid for docid in docids if qrels[qid].get(docid, 0) < relevance_level]
            for qid, docids in self.qid_to_docids.items()
        }

        self.total_samples = 0
        self.clean()

    def clean(self):
        # remove any ids that do not have any relevant docs or any non-relevant docs for training
        total_samples = 1  # keep tracks of the total possible number of unique training triples for this dataset
        for qid in list(self.qid_to_docids.keys()):
            posdocs = len(self.qid_to_reldocs[qid])
            negdocs = len(self.qid_to_negdocs[qid])
            total_samples += posdocs * negdocs
            if posdocs == 0 or negdocs == 0:
                logger.debug("removing training qid=%s with %s positive docs and %s negative docs", qid, posdocs,
                             negdocs)
                del self.qid_to_reldocs[qid]
                del self.qid_to_docids[qid]
                del self.qid_to_negdocs[qid]

        self.total_samples = total_samples

    def get_hash(self):
        raise NotImplementedError

    def get_total_samples(self):
        return self.total_samples

    def generate_samples(self):
        raise NotImplementedError


@Sampler.register
class TrainTripletSampler(Sampler, torch.utils.data.IterableDataset):
    """
    Samples training data triplets. Each samples is of the form (query, relevant doc, non-relevant doc)
    """

    module_name = "triplet"
    config_spec = [
        ConfigOption("seed", 1234),
    ]
    dependencies = []

    def __hash__(self):
        return self.get_hash()

    def get_hash(self):
        sorted_rep = sorted([(qid, docids) for qid, docids in self.qid_to_docids.items()])
        key_content = "{0}{1}".format(self.extractor.get_cache_path(), str(sorted_rep))
        key = hashlib.md5(key_content.encode("utf-8")).hexdigest()
        return "triplet_{0}".format(key)

    def generate_samples(self):
        """
        Generates triplets infinitely.
        """
        all_qids = sorted(self.qid_to_reldocs)
        if len(all_qids) == 0:
            raise RuntimeError("TrainDataset has no valid qids")

        random.seed(self.config["seed"])
        while True:
            random.shuffle(all_qids)

            # TODO: Investigate if co-locating samples for a query improves performance
            # Right now a batch of 32 samples will contain 32 different qids. Not sure if this is a good thing.
            for qid in all_qids:
                posdocid = random.choice(self.qid_to_reldocs[qid])
                negdocid = random.choice(self.qid_to_negdocs[qid])

                try:
                    yield self.extractor.id2vec(qid, posdocid, negdocid, label=[1, 0])
                except MissingDocError:
                    # at training time we warn but ignore on missing docs
                    logger.warning(
                        "skipping training pair with missing features: qid=%s posid=%s negid=%s", qid, posdocid, negdocid
                    )

    def __iter__(self):
        """
        Returns: Triplets of the form (query_feature, posdoc_feature, negdoc_feature)
        """

        return iter(self.generate_samples())


@Sampler.register
class TrainPairSampler(Sampler, torch.utils.data.IterableDataset):
    """
    Samples training data pairs. Each sample is of the form (query, doc)
    Iterates through all the relevant documents in qrels
    """
    module_name = "pair"
    config_spec = [
        ConfigOption("seed", 1234),
    ]
    dependencies = []

    def get_hash(self):
        sorted_rep = sorted([(qid, docids) for qid, docids in self.qid_to_docids.items()])
        key_content = "{0}{1}".format(self.extractor.get_cache_path(), str(sorted_rep))
        key = hashlib.md5(key_content.encode("utf-8")).hexdigest()
        return "pair_{0}".format(key)

    def generate_samples(self):
        all_qids = sorted(self.qid_to_reldocs)
        if len(all_qids) == 0:
            raise RuntimeError("TrainDataset has no valid training pairs")

        random.seed(self.config["seed"])

        while True:
            # TODO: two documents does not necessarily come from same query
            random.shuffle(all_qids)
            for qid in all_qids:
                for docid in self.qid_to_reldocs[qid]:
                    yield self.extractor.id2vec(qid, docid, negid=None, label=[0, 1])
                for docid in self.qid_to_negdocs[qid]:
                    yield self.extractor.id2vec(qid, docid, negid=None, label=[1, 0])

    def __iter__(self):
        return iter(self.generate_samples())


@Sampler.register
class PredSampler(Sampler, torch.utils.data.IterableDataset):
    """
    Creates a Dataset for evaluation (test) data to be used with a pytorch DataLoader
    """
    module_name = "pred"
    config_spec = [
        ConfigOption("seed", 1234),
    ]

    def get_hash(self):
        sorted_rep = sorted([(qid, docids) for qid, docids in self.qid_to_docids.items()])
        key_content = "{0}{1}".format(self.extractor.get_cache_path(), str(sorted_rep))
        key = hashlib.md5(key_content.encode("utf-8")).hexdigest()

        return "dev_{0}".format(key)

    def generate_samples(self):
        for qid, docids in self.qid_to_docids.items():
            for docid in docids:
                try:
                    if docid in self.qid_to_reldocs[qid]:
                        yield self.extractor.id2vec(qid, docid, label=[0, 1])
                    else:
                        yield self.extractor.id2vec(qid, docid, label=[1, 0])
                except MissingDocError:
                    # when predictiong we raise an exception on missing docs, as this may invalidate results
                    logger.error("got none features for prediction: qid=%s posid=%s", qid, docid)
                    raise

    def __hash__(self):
        return self.get_hash()

    def __iter__(self):
        """
        Returns: Tuples of the form (query_feature, posdoc_feature)
        """

        return iter(self.generate_samples())

    def get_qid_docid_pairs(self):
        """
        Returns a generator for the (qid, docid) pairs. Useful if you want to sequentially access the pred pairs without
        extracting the actual content
        """
        for qid in self.qid_to_docids:
            for docid in self.qid_to_docids[qid]:
                yield qid, docid
