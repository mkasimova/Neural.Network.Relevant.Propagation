from __future__ import absolute_import, division, print_function

import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format='%(asctime)s %(name)s-%(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')

from modules import feature_extraction as fe, postprocessing as pp, visualization
from modules.data_generation import DataGenerator

logger = logging.getLogger("dataGenNb")


def run(dg, data, labels, supervised=True, filetype="svg", n_iterations=10, variance_cutoff="1_components"):
    cluster_indices = labels.argmax(axis=1)
    feature_to_resids = dg.feature_to_resids()
    suffix = dg.test_model + "_" + dg.feature_type \
             + ("_supervised" if supervised else "_unsupervised") \
             + ("_var-cutoff=" + str(variance_cutoff) if not supervised else "")
    kwargs = {
        'samples': data,
        'cluster_indices': cluster_indices,
        'filter_by_distance_cutoff': False,
        'use_inverse_distances': True,
        'n_splits': 1,
        'n_iterations': n_iterations,
        # 'upper_bound_distance_cutoff': 1.,
        # 'lower_bound_distance_cutoff': 1.
    }

    supervised_feature_extractors = [
        fe.MlpFeatureExtractor(
            activation="relu",
            classifier_kwargs={
                'hidden_layer_sizes': (dg.natoms, dg.nclusters * 2),
                # 'hidden_layer_sizes': [int(min(dg.nfeatures, 100) / (i + 1)) for i in range(10)],
                'max_iter': 10000,
                'alpha': 0.0001,
            },
            **kwargs),
        fe.ElmFeatureExtractor(
            activation="relu",
            classifier_kwargs={
                'hidden_layer_sizes': (dg.nfeatures,),
                'alpha': 50,
            },
            **kwargs),
        fe.KLFeatureExtractor(**kwargs),
        fe.RandomForestFeatureExtractor(
            one_vs_rest=False,
            classifier_kwargs={'n_estimators': 1000},
            **kwargs),
    ]
    unsupervised_feature_extractors = [
        fe.MlpAeFeatureExtractor(
            classifier_kwargs={
                # hidden_layer_sizes=(int(data.shape[1]/2),),
                # 'hidden_layer_sizes': (200, 100, 30, dg.nclusters, 30, 100, 200,),
                'hidden_layer_sizes': (10, 5, 1, 5, 10,),
                # hidden_layer_sizes=(100, 1, 100,),
                # hidden_layer_sizes=(200, 50, 10, 1, 10, 50, 200, ),
                'max_iter': 100000,
                # hidden_layer_sizes=(300, 200, 50, 10, 1, 10, 50, 200, 300,),
                # max_iter=10000,
                # 'alpha': 0.0001,
                'alpha': 1,
                'solver': "adam",
            },
            use_reconstruction_for_lrp=True,
            activation="logistic",
            **kwargs),
        fe.PCAFeatureExtractor(classifier_kwargs={'n_components': None},
                               variance_cutoff=variance_cutoff,
                               name='PCA',
                               **kwargs),
        fe.RbmFeatureExtractor(classifier_kwargs={'n_components': dg.nclusters},
                               relevance_method='from_lrp',
                               name='RBM',
                               **kwargs),
    ]
    feature_extractors = supervised_feature_extractors if supervised else unsupervised_feature_extractors
    logger.info("Done. using %s feature extractors", len(feature_extractors))

    results = []
    for extractor in feature_extractors:
        extractor.error_limit = 50
        logger.info("Computing relevance for extractors %s", extractor.name)
        extractor.extract_features()
        test_set_errors = extractor.test_set_errors
        feature_importance = extractor.feature_importance
        std_feature_importance = extractor.std_feature_importance

        # logger.info("Get feature_importance and std of shapes %s, %s", feature_importance.shape, std_feature_importance.shape)
        results.append((extractor, feature_importance, std_feature_importance, test_set_errors))
    logger.info("Done")

    postprocessors = []
    filter_results = True
    for (extractor, feature_importance, std_feature_importance, errors) in results:
        p = pp.PostProcessor(extractor,
                             working_dir=".",
                             pdb_file=None,
                             feature_to_resids=feature_to_resids,
                             filter_results=filter_results)
        p.average()
        p.evaluate_performance()
        # p.persist()
        postprocessors.append([p])
    logger.info("Done")

    logger.info(
        "Actual atoms moved: %s.\n(Cluster generation method %s. Noise level=%s, displacement=%s. frames/cluster=%s)",
        sorted(dg.moved_atoms),
        dg.test_model, dg.noise_level, dg.displacement, dg.nframes_per_cluster)

    visualization.visualize(postprocessors,
                            show_importance=True,
                            show_performance=False,
                            show_projected_data=False,
                            highlighted_residues=dg.moved_atoms,
                            color_offset=0 if supervised else 3,
                            outfile="output/test_importance_per_residue_{suffix}.{filetype}".format(suffix=suffix,
                                                                                                    filetype=filetype))
    visualization.visualize(postprocessors,
                            show_importance=False,
                            show_performance=True,
                            show_projected_data=False,
                            outfile="output/test_performance_{suffix}.{filetype}".format(suffix=suffix,
                                                                                         filetype=filetype))
    visualization.visualize(postprocessors,
                            show_importance=False,
                            show_performance=False,
                            show_projected_data=True,
                            outfile="output/test_projection_{suffix}.{filetype}".format(suffix=suffix,
                                                                                        filetype=filetype))
    logger.info("Done. The settings were n_iterations = {n_iterations}, n_splits = {n_splits}."
                "\nFiltering (filter_by_distance_cutoff={filter_by_distance_cutoff})".format(**kwargs))


if __name__ == "__main__":
    dg = DataGenerator(
        natoms=20,
        nclusters=3,
        natoms_per_cluster=[1, 1, 1],
        moved_atoms=[[10], [12], [14]],
        # natoms=200,
        # nclusters=4,
        # natoms_per_cluster=[1, 1, 1, 1],
        # moved_atoms=[[10], [60], [110], [130]],
        nframes_per_cluster=2000,
        noise_level=0.005,  # 1e-2, #1e-2,
        displacement=0.5,
        noise_natoms=0,
        feature_type='inv-dist',
        test_model='non-linear'
        # test_model='non-linear'
        # test_model='non-linear-random-displacement'
    )
    data, labels = dg.generate_data(
        xyz_output_dir=None)
    # "output/xyz/{}_{}_{}atoms_{}clusters".format(dg.test_model, dg.feature_type, dg.natoms, dg.nclusters))
    logger.info("Generated data of shape %s and %s clusters", data.shape, labels.shape[1])
    run(dg, data, labels, supervised=True, n_iterations=5)
