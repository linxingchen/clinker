#!/usr/bin/env python3

"""
translation protein sequences in Clusters or lists of Proteins.

Cameron Gilchrist
"""

import logging
import io

from collections import defaultdict, OrderedDict
from itertools import combinations, product
from operator import attrgetter

import numpy as np

from scipy.cluster import hierarchy
from scipy.spatial.distance import squareform

from Bio import Align
from Bio.Align import substitution_matrices

from clinker.formatters import format_alignment, format_globaligner
from clinker.classes import Cluster, Serializer


LOG = logging.getLogger(__name__)


def align_clusters(*args, cutoff=0.3, aligner_config=None):
    """Convenience function for directly aligning Cluster object/s.

    Initialises a Globaligner, adds Cluster/s, then runs alignments
    and returns the Globaligner.

    Args:
        *args: Cluster or list of Protein objects
        aligner_config (dict): keyword arguments to use when setting
                               up the BioPython.PairwiseAligner object
        cutoff (float): decimal identity cutoff for saving an alignment
    Returns:
        aligner (Globaligner): instance of Globaligner class which
                                  contains all cluster alignments
    e.g.
        align_sequence_groups(cluster1, cluster2, ..., clusterN)
    """
    aligner = Globaligner(aligner_config)
    aligner.add_clusters(*args)
    if len(args) == 1:
        LOG.info("Only one cluster given, skipping alignment")
    else:
        aligner.align_stored_clusters(cutoff)
    return aligner


def assign_groups(links, threshold=0.3):
    """Groups sequences in alignment links by single-linkage."""
    groups = []
    for link in links:
        if link.identity < threshold:
            continue
        found = False
        for (i, group) in enumerate(groups):
            if link.query in group or link.target in group:
                found = True
            if found:
                for gene in [link.query, link.target]:
                    if gene not in group:
                        group.append(gene)
                        gene._group = i
                break
        if not found:
            groups.append([link.query, link.target])
            index = len(groups) - 1
            link.query._group = index
            link.target._group = index


def get_pairs(cluster):
    """Gets all contiguous pairs of homology groups in a cluster."""
    pairs = []
    for locus in cluster.loci:
        total = len(locus.genes) - 1
        pairs.extend(
            (gene._group for gene in locus.genes[i:i+2])
            for i in range(total)
        )
    return pairs


def compare_pairs(one, two):
    """Compares two collections of contiguous group pairs.

    Gets common elements (i.e. intersection) between each list, and then
    finds the minimum number of occurrences of the elements in either,
    such that shared duplicate pairs will be included in the total.
    """
    total = 0
    for pair in set(one).intersection(two):
        total += min(one.count(pair), two.count(pair))
    return total


def compute_identity(alignment):
    """Calculates sequence identity/similarity of a BioPython alignment object."""
    # Aligned strings aren't stored separately, have to split
    one, _, two, _ = str(alignment).split("\n")
    length = len(one)

    # Amino acid similarity groups
    similar_acids = [
        {"G", "A", "V", "L", "I"},
        {"F", "Y", "W"},
        {"C", "M"},
        {"S", "T"},
        {"K", "R", "H"},
        {"D", "E", "N", "Q"},
        {"P"},
    ]

    matches, similar = 0, 0
    for i in range(length):
        if one[i] == two[i]:
            # Check for gap columns
            if one[i] not in {"-", "."}:
                matches += 1
            else:
                length -= 1
        else:
            # If not identical, check if similar
            for group in similar_acids:
                if one[i] in group and two[i] in group:
                    similar += 1
                    break

    # identity = matches / length - gaps
    # similarity = (matches + similarities) / length - gaps
    return matches / length, (matches + similar) / length


def load_substitution_matrix(matrix):
    text = matrix.encode("utf-8")
    byte = io.BytesIO(text)
    reader = io.BufferedReader(byte)
    wrapper = io.TextIOWrapper(reader)
    return substitution_matrices.read(wrapper)


def serialise_aligner_config(config):
    copy = config.copy()
    for key, value in config.items():
        copy[key] = value if isinstance(value, (float, int)) else str(value)
    return copy


def load_aligner_config(config):
    for key, value in config.items():
        if key == "substitution_matrix":
            config[key] = load_substitution_matrix(value)
    return config


class Globaligner(Serializer):
    """Performs and stores alignments.

    Parameters:
        aligner (Bio.Align.PairwiseAligner): Sqeuence aligner
        alignments (list): Alignments generated by Globaligner
        clusters (dict): Ordered dictionary of Clusters keyed on name
        _alignment_indices (dict): indices of Alignments in _alignments
            stored using Cluster.name attributes as keys
        _cluster_names (dict): tuples of Cluster.name attributes stored using
            _alignment indices as keys
    """

    aligner_default = {
        "mode": "global",
        "substitution_matrix": substitution_matrices.load("BLOSUM62"),
        "open_gap_score": -10,
        "extend_gap_score": -0.5,
    }

    def __init__(self, aligner_config=None):
        self.alignments = []
        self.aligner = Align.PairwiseAligner()
        self.clusters = OrderedDict()

        self._alignment_indices = defaultdict(dict)
        self._cluster_names = defaultdict(dict)

        self.aligner_config = self.aligner_default
        self.configure_aligner(**self.aligner_config)

        if aligner_config:
            self.configure_aligner(**aligner_config)

    def __str__(self):
        """Print all alignments currently stored in the instance."""
        return self.format()

    def format(
        self,
        delimiter=None,
        decimals=4,
        alignment_headers=True,
        link_headers=False,
    ):
        return format_globaligner(
            self,
            decimals=decimals,
            delimiter=delimiter,
            alignment_headers=alignment_headers,
            link_headers=link_headers,
        )

    def to_data(self, i=0.5, method="ward"):
        """Formats Globaligner as plottable data set.

        Assign unique indices to all clusters, loci, genes
        """
        clusters = list(self.clusters.values())
        return {
            "clusters": [
                clusters[i].to_dict()
                for i in self.order(i=i, method=method)
            ],
            "links": [
                link.to_dict()
                for alignment in self.alignments
                for link in alignment.links
            ],
        }

    def to_dict(self):
        return dict(
            clusters=[c.to_dict() for c in self.clusters.values()],
            aligner_config=serialise_aligner_config(self.aligner_config),
            alignments=[a.to_dict() for a in self.alignments],
            _alignment_indices=dict(self._alignment_indices),
            _cluster_names=dict(self._cluster_names),
        )

    @classmethod
    def from_dict(cls, d):
        config = load_aligner_config(d["aligner_config"])
        g = cls(aligner_config=config)
        g._alignment_indices = defaultdict(dict, d["_alignment_indices"])
        g._cluster_names = defaultdict(dict, d["_cluster_names"])
        g.clusters = OrderedDict((c["name"], Cluster.from_dict(c)) for c in d["clusters"])
        g.alignments = [Alignment.from_dict(a) for a in d["alignments"]]
        return g

    def add_clusters(self, *clusters):
        """Adds new Cluster object/s to the Globaligner.

        Parameters:
            clusters (list): variable number of Cluster objects
        """
        for cluster in clusters:
            if not isinstance(cluster, Cluster):
                raise NotImplementedError("Expected Cluster object")
            if cluster.name not in self.clusters:
                self.clusters[cluster.name] = cluster

    def align_clusters(self, one, two, cutoff=0.3):
        """Constructs a cluster alignment using aligner in the Globaligner."""
        alignment = Alignment(query=one, target=two)
        for locusA, locusB in product(one.loci, two.loci):
            for geneA, geneB in product(locusA.genes, locusB.genes):
                aln = self.aligner.align(geneA.translation, geneB.translation)
                identity, similarity = compute_identity(aln[0])
                if identity < cutoff:
                    continue
                alignment.add_link(geneA, geneB, identity, similarity)
        return alignment

    def align_stored_clusters(self, cutoff=0.3):
        """Aligns clusters stored in the Globaligner."""
        for one, two in combinations(self.clusters.values(), 2):
            if self._alignment_indices[one.name].get(two.name):
                continue
            LOG.info("%s vs %s", one.name, two.name)
            alignment = self.align_clusters(one, two, cutoff)
            self.add_alignment(alignment)

    def configure_aligner(self, **kwargs):
        """Change properties on the BioPython.PairwiseAligner object.

        This function takes any keyword argument and assumes
        they correspond to valid properties on the PairwiseAligner.
        Refer to BioPython documentation for these.
        """
        valid_attributes = set(dir(self.aligner))
        for key, value in kwargs.items():
            if key not in valid_attributes:
                raise ValueError(
                    f'"{key}" is not a valid attribute of the BioPython'
                    "Align.PairwiseAligner class"
                )
            self.aligner_config[key] = value
            setattr(self.aligner, key, value)

    @property
    def aligner_settings(self):
        """Returns a printout of the current PairwiseAligner object settings."""
        return str(self.aligner)

    def form_alignment_string(self, index):
        """Return a string representation of a stored Alignment."""
        one, two = self._cluster_names[index]
        header = f"{one} vs {two}"
        separator = "-" * len(header)
        alignment = self.alignments[index]
        return f"{header}\n{separator}\n{alignment}"

    def add_alignment(self, alignment):
        """Adds a new cluster alignment to the Globaligner.

        self._alignment_indices allows for Alignment indices to be
        retrieved from cluster names, regardless of order.

        self._cluster_names allows for Cluster names to be retrieved
        given the index of an Alignment in self.alignments
        """
        # Save Cluster object if not already stored
        q = alignment.query
        t = alignment.target
        self.add_clusters(q, t)

        # Overwrite previous alignment between these clusters if one exists
        index = self._alignment_indices[q.name].get(t.name)
        if index:
            self.alignments[index] = alignment
        else:
            index = len(self.alignments)
            self.alignments.append(alignment)

        # Update mapping dictionaries and save Alignment
        self._alignment_indices[q.name][t.name] = index
        self._alignment_indices[t.name][q.name] = index
        self._cluster_names[index] = (q.name, t.name)

    def get_alignment(self, one, two):
        """Retrieves an Alignment corresponding to two Cluster objects.

        Parameters:
            one (str): Name of first cluster
            two (str): Name of second cluster
        Returns:
            Alignment object for the specified Clusters
        """
        index = self._alignment_indices[one].get(two)
        if index is None:
            raise KeyError(f"No alignment stored for {one} and {two}")
        return self.alignments[index]

    def synteny(self, one, two, i=0.5):
        """Calculates a synteny score between two clusters.

        Based on antiSMASH/MultiGeneBlast implementation:
            S = h + i*s
        where:
            h = #homologues over minimum identity/coverage threshold
            s = #contiguous gene pairs
            i = weighting factor for s

        Except instead of counting number of homologues, we use a cumulative
        identity value of homologues in each cluster.
        """
        alignment = self.get_alignment(one, two)
        homology = sum(link.identity for link in alignment.links)

        assign_groups(alignment.links)
        one_cluster = self.clusters[one]
        two_cluster = self.clusters[two]
        one_pairs = get_pairs(one_cluster)
        two_pairs = get_pairs(two_cluster)
        contiguity = compare_pairs(one_pairs, two_pairs)

        return homology + i * contiguity

    def matrix(self, i=0.5, normalise=False, as_distance=False):
        """Generates a synteny score matrix of all aligned clusters.

        Arguments:
            i (float): Weighting of gene pair contiguity in synteny scores
            normalise (bool): Normalise the matrix (i.e. 0 to 1)
            as_distance (bool): Convert to distance matrix
        Returns:
            matrix (np.array): Synteny matrix
        """
        total = len(self.clusters)
        matrix = np.zeros((total, total))
        for i, one in enumerate(self.clusters):
            for j, two in enumerate(self.clusters):
                if i == j:
                    continue
                matrix[i, j] = self.synteny(one, two, i=i)
        if normalise:
            matrix /= matrix.max()
        if as_distance:
            maximum = 1 if normalise else matrix.max()
            matrix = maximum - matrix
            np.fill_diagonal(matrix, 0)
        return matrix

    def order(self, i=0.5, method="ward"):
        """Determines optimal order of clusters using hierarchical clustering.

        When only a single cluster is stored, skips clustering and returns 0.
        """
        if len(self.clusters) == 1:
            return [0]
        matrix = self.matrix(i=i, normalise=True, as_distance=True)
        linkage = hierarchy.linkage(squareform(matrix), method=method)
        return hierarchy.leaves_list(linkage)[::-1]


class Alignment(Serializer):
    """An alignment between two gene clusters.

    Attributes:
        links (list): list of Gene-Gene 'links' (i.e. alignments)
    """

    def __init__(self, query=None, target=None, links=None):
        self.query = query
        self.target = target
        self.links = links if links else []

    def __str__(self):
        return self.format()

    def to_dict(self):
        return dict(
            query=self.query.uid,
            target=self.target.uid,
            links=[link.to_dict() for link in self.links]
        )

    @classmethod
    def from_dict(cls, d):
        return cls(
            query=d.get("query"),
            target=d.get("target"),
            links=[Link.from_dict(link) for link in d.get("links")]
        )

    def format(
        self,
        decimals=4,
        delimiter=None,
        alignment_headers=True,
        link_headers=False,
    ):
        return format_alignment(
            self,
            decimals=decimals,
            delimiter=delimiter,
            alignment_headers=alignment_headers,
            link_headers=link_headers,
        )

    def contains(self, gene):
        """Return True if the given gene is in this cluster alignment."""
        return any(gene in (link.query, link.target) for link in self.links)

    @property
    def score(self):
        """Calculates the cumulative identity of this alignment."""
        if not self.links:
            raise ValueError("Alignment has no links")
        total = sum(link.identity for link in self.links)
        count = len(self.links)
        return total / count

    def add_link(self, query, target, identity, similarity):
        """Instantiate a new Link from a Gene alignment and save."""
        link = Link(query, target, identity=identity, similarity=similarity)
        self.links.append(link)


class Link(Serializer):
    """An alignment link between two Gene objects."""

    def __init__(self, query, target, identity=None, similarity=None):
        self.query = query
        self.target = target
        self.identity = identity
        self.similarity = similarity

    def __str__(self):
        return self.format("\t")

    def values(self):
        return [self.query.name, self.target.name, self.identity, self.similarity]

    def to_dict(self):
        return {
            "query": self.query.uid,
            "target": self.target.uid,
            "identity": self.identity,
            "similarity": self.similarity,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)
