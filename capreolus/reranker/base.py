import os
import tensorflow as tf
import pickle

from profane import ConfigOption, Dependency, ModuleBase


class Reranker(ModuleBase):
    module_type = "reranker"
    dependencies = [
        Dependency(key="extractor", module="extractor", name="embedtext"),
        Dependency(key="trainer", module="trainer", name="pytorch"),
    ]

    def add_summary(self, summary_writer, niter):
        """
        Write to the summay_writer custom visualizations/data specific to this reranker
        """
        for name, weight in self.model.named_parameters():
            summary_writer.add_histogram(name, weight.data.cpu(), niter)
            # summary_writer.add_histogram(f'{name}.grad', weight.grad, niter)

    def save_weights(self, weights_fn, optimizer):
        if not os.path.exists(os.path.dirname(weights_fn)):
            os.makedirs(os.path.dirname(weights_fn))

        d = {k: v for k, v in self.model.state_dict().items() if ("embedding.weight" not in k and "_nosave_" not in k)}
        with open(weights_fn, "wb") as outf:
            pickle.dump(d, outf, protocol=-1)

        optimizer_fn = weights_fn.as_posix() + ".optimizer"
        with open(optimizer_fn, "wb") as outf:
            pickle.dump(optimizer.state_dict(), outf, protocol=-1)

    def load_weights(self, weights_fn, optimizer):
        with open(weights_fn, "rb") as f:
            d = pickle.load(f)

        cur_keys = set(k for k in self.model.state_dict().keys() if not ("embedding.weight" in k or "_nosave_" in k))
        missing = cur_keys - set(d.keys())
        if len(missing) > 0:
            raise RuntimeError("loading state_dict with keys that do not match current model: %s" % missing)

        self.model.load_state_dict(d, strict=False)

        optimizer_fn = weights_fn.as_posix() + ".optimizer"
        with open(optimizer_fn, "rb") as f:
            optimizer.load_state_dict(pickle.load(f))


class KerasModel(tf.keras.Model):
    """
    Wrapper class for Keras models. Handles the invocation of call() based on whether the training
    input is a triplet or a pair
    """
    def __init__(self, model, config, *args, **kwargs):
        super(KerasModel, self).__init__(*args, **kwargs)
        self.model = model
        self.config = config

    def call(self, x, **kwargs):
        posdoc, negdoc, query, additional = x[0], x[1], x[2], x[3:]

        def score_pos():
            return self.model((posdoc, query, ) + tuple(additional))

        def score_pos_and_neg():
            pos_score = self.model((posdoc, query) + tuple(additional))
            neg_score = self.model((negdoc, query) + tuple(additional))

            return tf.stack([pos_score, neg_score], axis=1)

        # If the negdoc is a zero tensor, score only the positive document
        # If the negdoc tensor is zero, it could either mean:
        #   1. The training input is pairwise (eg: when using cross-entropy loss) instead of triplets (eg: pairwise-hinge loss)
        #   2. We are predicting on a validation/dev set
        result = tf.cond(tf.equal(tf.math.count_nonzero(negdoc), 0), score_pos, score_pos_and_neg)

        return result
