from __future__ import absolute_import, division, print_function

import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format='%(asctime)s %(name)s-%(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
import os
import numpy as np
from biopandas.pdb import PandasPdb
import modules.utils as utils
from scipy.spatial.distance import squareform

logger = logging.getLogger("postprocessing")


class PostProcessor(object):

    def __init__(self, extractor, feature_importance, std_feature_importance, cluster_indices, working_dir,
                 feature_to_resids=None):
        """
        Class which computes all the necessary averages and saves them as fields
        TODO move some functionality from class feature_extractor here
        :param extractor:
        :param feature_importance:
        :param std_feature_importance:
        :param cluster_indices:
        :param working_dir:
        :param feature_to_resids: an array of dimension nfeatures*2 which tells which two residues are involved in a feature
        """
        self.working_dir = working_dir
        self.cluster_indices = cluster_indices
        self.std_feature_importance = std_feature_importance
        self.feature_importance = feature_importance
        self.extractor = extractor
        self.nfeatures, self.nclusters = feature_importance.shape
        if feature_to_resids is None:
            feature_to_resids = utils.get_default_feature_to_resids(self.nfeatures)
        self.feature_to_resids = feature_to_resids
        self.importance_per_cluster = None
        self.importance_per_residue_and_cluster = None
        self.importance_per_residue = None
        self.index_to_resid = None

    def average(self):
        """
        Computes average importance per cluster and residue and residue etc.
        Sets the fields importance_per_cluster, importance_per_residue_and_cluster, importance_per_residue
        :return: itself
        """
        self.importance_per_cluster = self.feature_importance  # compute_importance_per_cluster(importance, cluster_indices)
        self._compute_importance_per_residue_and_cluster()
        self._compute_importance_per_residue()
        return self

    def persist(self, directory=None):
        """
        Save .npy files of the different averages and pdb files with the beta column set to importance
        :return: itself
        """
        if directory is None:
            directory = self.working_dir + "analysis/{}/".format(self.extractor.name)
        if not os.path.exists(directory):
            os.makedirs(directory)
        np.save(directory + "importance_per_cluster", self.importance_per_cluster)
        np.save(directory + "importance_per_residue_and_cluster", self.importance_per_residue_and_cluster)
        np.save(directory + "importance_per_residue", self.importance_per_residue)

        if not os.path.exists(self.working_dir + "analysis/"):
            os.makedirs(self.working_dir + "analysis/")
		
        pdb_file = self.working_dir + "analysis/all.pdb"
        pdb = PandasPdb()
        pdb.read_pdb(pdb_file)
        _save_to_pdb(pdb, directory + "all_importance.pdb",
                     self._map_to_correct_residues())
        for cluster_idx, importance in enumerate(self.importance_per_residue_and_cluster.T):
            _save_to_pdb(pdb, directory + "cluster_{}_importance.pdb".format(cluster_idx),
                         self._map_to_correct_residues())
        return self

    def _compute_importance_per_residue_and_cluster(self):
        importance = self.importance_per_cluster
        if self.nclusters < 2:
            logger.debug("Not possible to compute importance per cluster")
        index_to_resid = set(self.feature_to_resids.flatten())  # at index X we have residue number
        self.nresidues = len(index_to_resid)
        index_to_resid = [r for r in index_to_resid]
        res_id_to_index = {}  # a map pointing back to the index in the array index_to_resid
        for idx, resid in enumerate(index_to_resid):
            res_id_to_index[resid] = idx
        importance_per_residue_and_cluster = np.zeros((self.nresidues, self.nclusters))
        for feature_idx, rel in enumerate(importance):
            res1, res2 = self.feature_to_resids[feature_idx]
            res1 = res_id_to_index[res1]
            res2 = res_id_to_index[res2]
            importance_per_residue_and_cluster[res1, :] += rel
            importance_per_residue_and_cluster[res2, :] += rel
            
        importance_per_residue_and_cluster, _ = rescale_feature_importance(importance_per_residue_and_cluster, None)
        self.importance_per_residue_and_cluster = importance_per_residue_and_cluster
        self.index_to_resid = index_to_resid

    def _compute_importance_per_residue(self):
        if len(self.importance_per_residue_and_cluster.shape) < 2:
            self.importance_per_residue = self.importance_per_residue_and_cluster
        else:
            self.importance_per_residue = self.importance_per_residue_and_cluster.mean(axis=1)

    def _map_to_correct_residues(self):
        residue_to_importance = {}
        for idx, rel in enumerate(self.importance_per_residue):
            resSeq = self.index_to_resid[idx]
            residue_to_importance[resSeq] = rel
        self._residue_to_importance = residue_to_importance
        return residue_to_importance


def _save_to_pdb(pdb, out_file, residue_to_importance):
    atom = pdb.df['ATOM']
    for i, line in atom.iterrows():
        resSeq = int(line['residue_number'])
        importance = residue_to_importance.get(resSeq, None)
        if importance is None:
            logger.warn("importance is None for residue %s and line %s", resSeq, line)
            continue
        atom.set_value(i, 'b_factor', importance)
    pdb.to_pdb(path=out_file, records=None, gz=False, append_newline=True)


def rescale_feature_importance(relevances, std_relevances):
    """
    Min-max rescale feature importances
    :param feature_importance: array of dimension nfeatures * nstates
    :param std_feature_importance: array of dimension nfeatures * nstates
    :return: rescaled versions of the inputs with values between 0 and 1
    """
    if len(relevances.shape) == 1:
        n_states = 1
        relevances = relevances[:, np.newaxis]
        std_relevances = std_relevances[:,np.newaxis]
    else:
        n_states = relevances.shape[1]

    n_features = relevances.shape[0]

    for i in range(n_states):
        max_val, min_val = relevances[:,i].max(), relevances[:,i].min()
        scale = max_val-min_val
        offset = min_val
        if scale < 1e-9:
            scale = 1.
        relevances[:,i] = (relevances[:,i] - offset)/scale
        if std_relevances is not None:
            std_relevances[:, i] /= scale #TODO correct?
    return relevances, std_relevances


def residue_importances(feature_importances, std_feature_importances):
    """
	Compute residue importance
	DEPRECATED method... Here in case we need to merge some of the functionality into the current method
	"""
    if len(feature_importances.shape) == 1:
        n_states = 1
        feature_importances = feature_importances[:, np.newaxis].T
        std_feature_importances = std_feature_importances[:, np.newaxis].T
    else:
        n_states = feature_importances.shape[0]

    n_residues = squareform(feature_importances[0, :]).shape[0]

    resid_importance = np.zeros((n_states, n_residues))
    std_resid_importance = np.zeros((n_states, n_residues))
    for i_state in range(n_states):
        resid_importance[i_state, :] = np.sum(squareform(feature_importances[i_state, :]), axis=1)
        std_resid_importance[i_state, :] = np.sqrt(np.sum(squareform(std_feature_importances[i_state, :] ** 2), axis=1))
    return resid_importance, std_resid_importance
