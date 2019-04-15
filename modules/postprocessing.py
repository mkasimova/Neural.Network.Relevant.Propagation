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
import mdtraj as md
from . import utils
from . import filtering
from . import data_projection as dp

logger = logging.getLogger("postprocessing")


class PostProcessor(object):

    def __init__(self,
                 extractor=None,
                 working_dir=None,
                 rescale_results=True,
                 filter_results=False,
                 feature_to_resids=None,
                 pdb_file=None,
                 predefined_relevant_residues=None,
                 use_GMM_estimator=True,
                 supervised=False):
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
        self.extractor = extractor
        self.feature_importances = extractor.feature_importance
        self.std_feature_importances = extractor.std_feature_importance
        self.supervised = extractor.supervised
        self.cluster_indices = extractor.cluster_indices
        self.nclusters = len(list(set(self.cluster_indices)))
        self.working_dir = working_dir
        if self.working_dir is None:
            self.working_dir = os.getcwd()
        self.pdb_file = pdb_file
        self.predefined_relevant_residues = predefined_relevant_residues
        self.use_GMM_estimator = use_GMM_estimator

        # Rescale and filter results if needed
        self.rescale_results = rescale_results
        if rescale_results:
            self.feature_importances, self.std_feature_importances = utils.rescale_feature_importance(
                self.feature_importances, self.std_feature_importances)
        if filter_results:
            self.feature_importances, self.std_feature_importances = filtering.filter_feature_importance(
                self.feature_importances, self.std_feature_importances)

        # Put importance and std to 0 for residues pairs which were filtered out during features filtering (they are set as -1 in self.feature_importances and self.std_feature_importances)
        self.indices_filtered = np.where(self.feature_importances[:, 0] == -1)[0]
        self.feature_importances[self.indices_filtered, :] = 0
        self.std_feature_importances[self.indices_filtered, :] = 0

        # Set mapping from features to residues
        self.nfeatures = self.feature_importances.shape[0]
        if feature_to_resids is None and self.pdb_file is None:
            feature_to_resids = utils.get_default_feature_to_resids(self.nfeatures)
        elif feature_to_resids is None and self.pdb_file is not None:
            feature_to_resids = utils.get_feature_to_resids_from_pdb(self.nfeatures, self.pdb_file)
        self.feature_to_resids = feature_to_resids

        # Set average feature importances to None
        self.importance_per_residue_and_cluster = None
        self.std_importance_per_residue_and_cluster = None
        self.importance_per_residue = None
        self.std_importance_per_residue = None
        self.index_to_resid = None

        # Performance metrics
        self.predefined_relevant_residues = predefined_relevant_residues
        self.average_std = None
        self.test_set_errors = extractor.test_set_errors.mean()
        self.data_projector = None
        self.tp_rate = None
        self.fp_rate = None
        self.auc = None

    def average(self):
        """
        Computes average importance per cluster and residue and residue etc.
        Sets the fields importance_per_residue_and_cluster, importance_per_residue
        :return: itself
        """
        self._map_feature_to_resids()
        self._compute_importance_per_residue()

        if self.supervised:
            self._compute_importance_per_residue_and_cluster()

        return self

    def evaluate_performance(self):
        """
        Computes -average of standard deviation (per residue)
                 -projection classification entropy
                 -area under ROC (for toy model only)
        """

        self._compute_average_std()
        self._compute_projection_classification_entropy()

        if self.supervised and self.predefined_relevant_residues is not None:
            self._compute_area_under_ROC()

        return self

    def persist(self):
        """
        Save .npy files of the different averages and pdb files with the beta column set to importance
        :return: itself
        """
        directory = self.working_dir + "/{}/".format(self.extractor.name)

        if not os.path.exists(directory):
            os.makedirs(directory)

        np.save(directory + "importance_per_residue", self.importance_per_residue)
        np.save(directory + "std_importance_per_residue", self.std_importance_per_residue)
        np.save(directory + "feature_importance", self.feature_importances)
        np.save(directory + "std_feature_importance", self.std_feature_importances)

        if self.importance_per_residue_and_cluster is not None and self.std_importance_per_residue_and_cluster is not None:
            np.save(directory + "importance_per_residue_and_cluster", self.importance_per_residue_and_cluster)
            np.save(directory + "std_importance_per_residue_and_cluster", self.std_importance_per_residue_and_cluster)

        if self.pdb_file is not None:
            pdb = PandasPdb()
            pdb.read_pdb(self.pdb_file)
            self._save_to_pdb(pdb, directory + "importance.pdb",
                              self._map_to_correct_residues(self.importance_per_residue))

            if self.importance_per_residue_and_cluster is not None:
                for cluster_idx, importance in enumerate(self.importance_per_residue_and_cluster.T):
                    self._save_to_pdb(pdb, directory + "cluster_{}_importance.pdb".format(cluster_idx),
                                      self._map_to_correct_residues(importance))

        return self

    def _map_feature_to_resids(self):

        self.index_to_resid = np.unique(
            np.asarray(self.feature_to_resids.flatten()))  # at index X we have residue number
        self.index_to_resid = [r for r in self.index_to_resid]
        self.nresidues = len(self.index_to_resid)

        res_id_to_index = {}  # a map pointing back to the index in the array index_to_resid
        for idx, resid in enumerate(self.index_to_resid):
            res_id_to_index[resid] = idx
        importance_mapped_to_resids = np.zeros((self.nresidues, self.feature_importances.shape[1]))
        std_importance_mapped_to_resids = np.zeros((self.nresidues, self.feature_importances.shape[1]))
        for feature_idx, rel in enumerate(self.feature_importances):
            res1, res2 = self.feature_to_resids[feature_idx]
            res1 = res_id_to_index[res1]
            res2 = res_id_to_index[res2]
            importance_mapped_to_resids[res1, :] += rel
            importance_mapped_to_resids[res2, :] += rel
            std_importance_mapped_to_resids[res1, :] += self.std_feature_importances[feature_idx, :] ** 2
            std_importance_mapped_to_resids[res2, :] += self.std_feature_importances[feature_idx, :] ** 2
        std_importance_mapped_to_resids = np.sqrt(std_importance_mapped_to_resids)

        self.importance_mapped_to_resids = importance_mapped_to_resids
        self.std_importance_mapped_to_resids = std_importance_mapped_to_resids

    def _compute_importance_per_residue(self):

        importance_per_residue = self.importance_mapped_to_resids.mean(axis=1)
        std_importance_per_residue = np.sqrt(np.mean(self.std_importance_mapped_to_resids ** 2, axis=1))

        if self.rescale_results:
            # Adds a second axis to feed to utils.rescale_feature_importance
            importance_per_residue = importance_per_residue.reshape((importance_per_residue.shape[0], 1))
            std_importance_per_residue = std_importance_per_residue.reshape((std_importance_per_residue.shape[0], 1))
            importance_per_residue, std_importance_per_residue = utils.rescale_feature_importance(
                importance_per_residue, std_importance_per_residue)
            importance_per_residue = importance_per_residue[:, 0]
            std_importance_per_residue = std_importance_per_residue[:, 0]

        self.importance_per_residue = importance_per_residue
        self.std_importance_per_residue = std_importance_per_residue

    def _compute_importance_per_residue_and_cluster(self):

        if self.rescale_results:
            self.importance_mapped_to_resids, self.std_importance_mapped_to_resids = utils.rescale_feature_importance(
                self.importance_mapped_to_resids, self.std_importance_mapped_to_resids)

        self.importance_per_residue_and_cluster = self.importance_mapped_to_resids
        self.std_importance_per_residue_and_cluster = self.std_importance_mapped_to_resids

    def _compute_average_std(self):
        """
        Computes average standard deviation
        """
        self.average_std = self.std_importance_per_residue.mean()

        return self

    def _compute_projection_classification_entropy(self):
        """
        Computes separation of clusters in the projected space given by the feature importances
        """
        self.data_projector = dp.DataProjector(self.extractor.samples, self.cluster_indices)
        self.data_projector.project(self.feature_importances).score_projection(use_GMM=self.use_GMM_estimator)

        return self

    '''
    def _compute_area_under_ROC(self):
        """
        Computes ROC curve and area under it
        """

        auc = 0
        for i in range(self.nclusters):

            actives = np.chararray(self.nresidues)
            actives[:] = 'd'
            ind_a = self.predefined_relevant_residues[i]
            actives[ind_a] = 'a'

            actives_len = len(ind_a)
            decoys_len = self.nresidues - actives_len

            ind_scores_sorted = np.argsort(-self.importance_per_residue_and_cluster[:,i])
            actives_sorted = actives[ind_scores_sorted]

            tp=0
            fp=0
            tp_rate = []
            fp_rate = []
            for j in actives_sorted:
                if j=='a':
                    tp+=1
                else:
                    fp+=1
                tp_rate.append(float(tp)/float(actives_len))
                fp_rate.append(float(fp)/float(decoys_len))

            for j in range(len(fp_rate)-1):
                auc += (fp_rate[j+1]-fp_rate[j])*(tp_rate[j+1]+tp_rate[j])/2

        self.auc = auc/self.nclusters
    '''

    def _compute_area_under_ROC(self):
        """
        Compute ( TP - FP )/( TP + FP ) and ( TN - FN ) / ( TN + FN )
        """

        POS_RATIO = 0
        NEG_RATIO = 0

        for i in range(self.nclusters):

            importance_per_residue_and_cluster_SORTED = -np.sort(-self.importance_per_residue_and_cluster[:, i])
            importance_per_residue_and_cluster_SORTED_IND = np.argsort(-self.importance_per_residue_and_cluster[:, i])

            difference = []
            for j in range(self.nresidues - 1):
                difference.append(
                    importance_per_residue_and_cluster_SORTED[j] - importance_per_residue_and_cluster_SORTED[j + 1])
            difference_max = max(difference)
            difference_max_index = difference.index(difference_max)

            POSITIVE = importance_per_residue_and_cluster_SORTED_IND[0:difference_max_index + 1]
            NEGATIVE = importance_per_residue_and_cluster_SORTED_IND[difference_max_index + 1:]

            ind_actives = self.predefined_relevant_residues[i]
            TP = len(list(set(POSITIVE) & set(ind_actives)))
            FP = len(POSITIVE) - TP

            ind_decoys = [j for j in np.arange(0, self.nresidues, 1) if j not in ind_actives]
            TN = len(list(set(NEGATIVE) & set(ind_decoys)))
            FN = len(NEGATIVE) - TN

            POS_RATIO += (TP - FP) / (TP + FP)
            NEG_RATIO += (TN - FN) / (TN + FN)

        self.auc = [POS_RATIO / self.nclusters, NEG_RATIO / self.nclusters]

    def _map_to_correct_residues(self, importance_per_residue):
        """
        Maps importances to correct residue numbers
        """
        residue_to_importance = {}
        for idx, rel in enumerate(importance_per_residue):
            resSeq = self.index_to_resid[idx]
            residue_to_importance[resSeq] = rel

        return residue_to_importance

    def _save_to_pdb(self, pdb, out_file, residue_to_importance):
        """
        Saves importances into beta column of pdb file
        """
        atom = pdb.df['ATOM']
        missing_residues = []
        for i, line in atom.iterrows():
            resSeq = int(line['residue_number'])
            importance = residue_to_importance.get(resSeq, None)
            if importance is None:
                missing_residues.append(resSeq)
                importance = 0
            atom.at[i, 'b_factor'] = importance
        if len(missing_residues) > 0:
            logger.warn("importance is None for residues %s", set(missing_residues))
        pdb.to_pdb(path=out_file, records=None, gz=False, append_newline=True)

        return self


class PerFrameImportancePostProcessor(PostProcessor):

    def __init__(self,
                 per_frame_importance_outfile=None,
                 frame_importances=None,
                 **kwargs):
        PostProcessor.__init__(self, **kwargs)
        self.per_frame_importance_outfile = per_frame_importance_outfile
        self.frame_importances = frame_importances

    def persist(self):
        PostProcessor.persist(self)
        if self.per_frame_importance_outfile is not None and \
                self.frame_importances is not None:
            with open(self.per_frame_importance_outfile, 'w') as of:
                logger.info("Writing per frame importance to file %s", self.per_frame_importance_outfile)
                self.to_vmd_file(of)

    def to_vmd_file(self, of):
        """
        writing VMD script, see https://www.ks.uiuc.edu/Research/vmd/mailing_list/vmd-l/5001.html
        :return:
        """
        if self.pdb_file is None:
            raise Exception("PDB file required to write per frame importance")

        # Map the feature to atoms for better performance
        top = md.load(self.pdb_file).top
        feature_to_atoms = []
        residue_to_atoms = {}
        for feature_idx, [res1, res2] in enumerate(self.feature_to_resids):
            atoms1 = residue_to_atoms.get(res1, None)
            if atoms1 is None:
                atoms1 = top.select("protein and resSeq {}".format(res1))
                residue_to_atoms[res1] = atoms1
            atoms2 = residue_to_atoms.get(res2, None)
            if atoms2 is None:
                atoms2 = top.select("protein and resSeq {}".format(res2))
                residue_to_atoms[res2] = atoms2
            feature_to_atoms.append(np.append(atoms1, atoms2))
        ##write to file in minibatches
        for frame_idx, importance in enumerate(self.frame_importances):
            # First normalize importance over features (not same as below)
            importance = (importance - importance.min()) / (importance.max() - importance.min() + 1e-6)
            # map importance to atom idx
            atom_to_importance = np.zeros((top.n_atoms))
            for feature_idx, atoms in enumerate(feature_to_atoms):
                fi = importance[feature_idx]
                for a in atoms:
                    atom_to_importance[a] += fi
            # Normalize to values between 0 and 1
            atom_to_importance = (atom_to_importance - atom_to_importance.min()) / \
                                 (atom_to_importance.max() - atom_to_importance.min() + 1e-6)
            # Go through atoms in sequential order
            lines = ["#Frame {}\n".format(frame_idx)] + ["{}\n".format(ai) for ai in atom_to_importance]
            of.writelines(lines)
