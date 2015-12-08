#!/usr/bin/env python2
"""
EMIRGE:
Expectation-Maximization Iterative Reconstruction of Genes from the Environment

Copyright (C) 2010-2012 Christopher S. Miller  (christopher.s.miller@ucdenver.edu)

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>

https://github.com/csmiller/EMIRGE

for help, type:
python emirge_amplicon.py --help
"""
USAGE = """usage: %prog DIR <required_parameters> [options]

This version of EMIRGE (%prog) attempts to reconstruct rRNA SSU genes
from Illumina amplicon data.  It can handle up to a few million rRNA
reads at a time.
DIR is the working directory to process data in.
Use --help to see a list of required and optional arguments

Additional information:
https://groups.google.com/group/emirge-users
https://github.com/csmiller/EMIRGE/wiki

If you use EMIRGE in your work, please cite these manuscripts, as appropriate.

Miller CS, Baker BJ, Thomas BC, Singer SW, Banfield JF (2011)
EMIRGE: reconstruction of full-length ribosomal genes from microbial community
short read sequencing data.
Genome biology 12: R44. doi:10.1186/gb-2011-12-5-r44.

Miller CS, Handley KM, Wrighton KC, Frischkorn KR, Thomas BC,
Banfield JF (2013)
Short-Read Assembly of Full-Length 16S Amplicons Reveals Bacterial Diversity in
Subsurface Sediments.
PloS one 8: e56018. doi:10.1371/journal.pone.0056018.
"""

import cPickle
import csv
import multiprocessing
import os
import re
import sys
from datetime import timedelta
from optparse import OptionParser, OptionGroup, SUPPRESS_HELP
from subprocess import Popen, PIPE, check_call
from time import ctime, time

import Emirge.amplicon as amplicon
import numpy
import pysam
from Bio import SeqIO
from Emirge.pykseq import Kseq
from scipy import sparse

from Emirge import io, log
from emirge_rename_fasta import rename

BOWTIE_l = 20
BOWTIE_e = 300

# currently, bowtie writes quals with an ascii offset of 33
BOWTIE_ASCII_OFFSET = 33


class EM(object):
    """
    driver class for EM algorithm
    """
    _VERBOSE = True
    base2i = {"A": 0, "T": 1, "C": 2, "G": 3, "D": 4}
    i2base = dict([(v, k) for k, v in base2i.iteritems()])
    # asciibase2i = {65:0,84:1,67:2,71:3}

    DEFAULT_ERROR = 0.05

    def __init__(self, reads1_filepath, reads2_filepath,
                 insert_mean,
                 insert_sd,
                 output_files_prefix,
                 cov_thresh,
                 n_cpus=1,
                 cwd=os.getcwd(),
                 max_read_length=76,
                 iterdir_prefix="iter.",
                 cluster_thresh=0.97,
                 indel_thresh=0.3,
                 mapping_nice=None,
                 reads_ascii_offset=64,
                 expected_coverage_thresh=10,
                 rewrite_reads=True):
        """
        n_cpus is how many processors to use for multithreaded steps
        (currently only the bowtie mapping)
        mapping_nice is nice value to add to mapping program
        """
        self.reads1_filepath = reads1_filepath
        self.reads2_filepath = reads2_filepath
        self.insert_mean = insert_mean
        self.insert_sd = insert_sd
        # add output_files_prefix to init args where
        # output_files_prefix = options.output_files_prefix
        self.output_files_prefix = output_files_prefix
        self.n_cpus = n_cpus
        self.mapping_nice = mapping_nice
        self.reads_ascii_offset = reads_ascii_offset

        self.iteration_i = None  # keeps track of which iteration we are on.
        self.cwd = cwd
        self.max_read_length = max_read_length
        self.iterdir_prefix = iterdir_prefix
        # if two sequences evolve to be >= cluster_thresh identical
        # (via vsearch), then merge them. [0, 1.0]
        self.cluster_thresh = cluster_thresh
        assert 0 <= self.cluster_thresh <= 1.0
        self.expected_coverage_thresh = expected_coverage_thresh
        self.indel_thresh = indel_thresh

        # Single numpy array.  Has the shape: (numsequences x numreads)
        # [numreads can be numpairs]
        self.likelihoods = None
        # = Pr(R_i|S_i),
        # the likelihood of generating read R_i given sequence S_i

        # list of numpy arrays.  list index is iteration number.
        # Each numpy array has the shape: (numsequences,)
        self.priors = []
        # = Pr(S_i),
        # the prior probability that sequence S generated any read

        # list of numpy arrays.  list index is iteration number.
        # Each numpy array has the shape: (numsequences x numreads)
        self.posteriors = []
        # = Pr(S_i|R_i),
        # the posterior probability that sequence S_i generated read R_i

        # dict's and list keeping id mappings between sequence names
        # and internal indices (seq_i)
        # index is stable between iterations.
        # If sequence_i2sequence value is None, means this sequence was
        # abandoned in a previous round
        self.sequence_name2sequence_i = {}
        # list index is iteration number:
        self.sequence_i2sequence_name = []

        # seq_i --> iteration first seen.  Useful for keeping track of
        # when a sequence first appeared, and not allowing merging of
        # recently split out sequences
        self.split_seq_first_appeared = {}

        # in fastq input (number of reads **or number of read pairs**)
        self.n_reads = 0
        self.n_reads_mapped = 0
        self.n_sequences = 0

        # other constants, potentially changeable, tunable later,
        # or could incorporate into probabilistic model:
        #
        # minimum depth to keep sequence around for next round
        self.min_depth = 5.0
        # minimum prior probability for a sequence to keep it
        # around for next round (alternative to depth, which is
        # a little weird when you allow mappings to more than one place.
        # NOT YET IMPLEMENTED
        self.min_prior = None

        # list of numpy arrays -- per base coverage values.
        self.base_coverages = []
        # EXPERIMENTAL:  Minimum coverage in order to be counted in
        #                min_length_coverage
        self.min_length_coverage_def = 1
        # EXPERIMENTAL.  Fraction of length that has to be covered by
        #                >= min_length_cov_depth
        self.min_length_coverage = None
        # if prob(N) for minor allele base N is >= this threshold, call
        # site a minor allele
        self.snp_minor_prob_thresh = 0.10
        # if >= this percentage of bases are minor alleles (according to
        # self.snp_minor_prob_thresh), then split this sequence into
        # two sequences.
        self.snp_percentage_thresh = 0.10

        # cov_thresh=options.min_coverage_threshold
        self.cov_thresh = cov_thresh

        if self.reads2_filepath is None:
            self.paired_end = False
        else:
            self.paired_end = True

        self.temporary_files = []  # to remove at end of run

        if rewrite_reads:
            (self.reads1, self.n_reads) = \
                io.ReindexReads(self.reads1_filepath)
            self.reads1_filepath = self.reads1.name
            if self.paired_end:
                (self.reads2, dummy) = \
                    io.ReindexReads(self.reads2_filepath)
                self.reads2_filepath = self.reads2.name
        else:
            # hidden option in main to avoid rewriting reads from big
            # files more than necessary if already has correct integer
            # read neames, then simply count reads in file
            self.n_reads = io.FastqCountReads(self.reads1_filepath)

        log.info("Number of reads (or read pairs) in input file(s): %d"
                 % self.n_reads)

        # bool vector indicating whether read n was ever seen mapped
        self.reads_seen = numpy.zeros(self.n_reads, dtype=numpy.uint8)

        # where 1st dimension is read index (from rewritten file headers)
        # and second dimension is read number (0 or 1 ==> read /1 or read /2)
        # 3rd dimension for reads and quals is max_readlen
        self.reads = numpy.empty((self.n_reads, 2, self.max_read_length),
                                 dtype=numpy.uint8)
        self.quals = numpy.empty_like(self.reads)
        self.readlengths = numpy.empty((self.n_reads, 2), dtype=numpy.uint16)
        # read through reads file again, fill these.
        amplicon.populate_reads_arrays(self)

    @log.timed("Reading bam file")
    def read_bam(self, bam_filename, reference_fasta_filename):
        """
        reads a bam file and...
        updates:
                self.sequence_i2sequence_name   # a numpy array
                self.sequence_name2sequence_i   # a dict
                self.read_name2read_i           # a dict
                self.probN

        doesn't do anything with these anymore, they should be populated and
        stable with _emirge.populate_reads_arrays
                self.reads
                self.quals
                self.readlengths

        creates a new EMPTY entry for (appends to list, removes t-2
                self.priors
                self.posteriors

        creates new each iteration (overwrites):
                self.likelihoods
                self.unmapped_bases
                self.coverage
                self.bamfile_data
                self.cigars

        This MUST maintain seq_i to name and read_i to name mappings between
        iterations, so that a single name always maintains the same index from
        one iteration to the next.  One result of this requirement is that the
        various matrices can always get larger in a later t, but never smaller
        (as reads or seqs are added)
        """

        self.current_bam_filename = bam_filename
        self.n_alignments = self.get_n_alignments_from_bowtie()
        self.current_reference_fasta_filename = reference_fasta_filename
        self.fastafile = pysam.Fastafile(self.current_reference_fasta_filename)
        self.cigars = []  # list of pysam cigartuples

        # populate in cython:
        #  self.sequence_name2sequence_i
        #  self.sequence_i2sequence_name
        #  self.bamfile_data  numpy array
        #     with (seq_i, read_i, pair_i, rlen, pos, is_reverse)
        #  self.cigars
        amplicon.process_bamfile(self, BOWTIE_ASCII_OFFSET)

        self.n_sequences = len(self.sequence_name2sequence_i)

        t_check = time()
        self.priors.append(numpy.zeros(self.n_sequences, dtype=numpy.float))
        self.likelihoods = sparse.coo_matrix(
            (self.n_sequences, self.n_reads), dtype=numpy.float
        )  # init all to zero.
        self.posteriors.append(sparse.lil_matrix(
            (self.n_sequences + 1, self.n_reads + 1), dtype=numpy.float)
        )

        # TODO: is this necessary any more?
        # or is bookkeeping with probN good enough now.
        self.probN = [None for x in range(self.n_sequences)]
        # adjusted initialization to be same as probN as was done in emirge.py
        self.prob_indels = [None for x in self.probN]
        self.unmapped_bases = [None for x in self.probN]
        # need to calculate this each time? can't we set this once as
        # self.readlengths doesn't change with iters?
        self.mean_read_length = numpy.mean(self.readlengths)

        # reset probN for valid sequences (from
        # current_reference_fasta_filename). is this still necessary?
        # Or do I keep probN bookkeeping in order already?
        t_check = time()
        amplicon.reset_probN(self)  # also updates coverage values and culls via fraction of length covered, NEW: resets prob_indels as well
        # print >> sys.stderr, "DEBUG: reset_probN loop time: %s"%(timedelta(seconds = time()-t_check))

        for d in [self.priors, self.posteriors]:
            if len(d) > 2:
                trash = d.pop(0)  # no longer care about t-2
                del trash

    @log.timed("Initializing EM")
    def initialize_EM(self, bam_filename, reference_fasta_filename, randomize_priors=False):
        """
        Set up EM with two things so that first iteration can proceed:
           - Initial guesses of Pr(S) are made purely based on read counts, where each read is only allowed to
             map only once to a single best reference  (**if more than one alignment reported per read, raise exception!**).
           - Initial guess of Pr(N=n) (necessary for likelihood in Pr(S|R) is also calculated simply, with the assumption
             of 1 read (the best again) mapped to exactly 1 sequence.  Thus Pr(N=n) only takes the base call errors
             into account.  This is actually not done here, but rather the first time self.calc_probN is called.

           - bamfile for iteration 0 is assumed to have just one ("best") mapping per read.
           - there is no t-1 for t = 0, hence the need to set up Pr(S)

           if randomize_priors == True, then after calculating priors,
           shuffle them randomly.  This is useful for debugging
           purposes, to test effect of initialization, robustness of
           results, and how often the algorithm gets stuck in local
           maxima.
        """

        self.iteration_i = -1
        self.read_bam(bam_filename, reference_fasta_filename)

        # initialize priors.  Here just adding a count for each read mapped to each reference sequence
        # since bowtie run with --best and reporting just 1 alignment at random, there is some stochasticity here.
        for (seq_i, read_i, pair_i, rlen, pos, is_reverse) in self.bamfile_data:
            # if self.probN[seq_i] is not None:
            self.priors[-1][seq_i] += 1

        # this shouldn't be necessary with way I do initial mapping right now (all seq_i in priors should be nonzero initially)
        nonzero_indices = numpy.nonzero(self.priors[-1])  # only divide cells with at least one count.  Set all others to Pr(S) = 0
        self.priors[-1] = self.priors[-1][nonzero_indices] / self.priors[-1][nonzero_indices].sum()  # turn these into probabilities

        if randomize_priors:
            numpy.random.shuffle(self.priors[-1])

        self.priors.append(self.priors[-1].copy())  # push this back to t-1 (index == -2)

        # write priors as special case:
        self.print_priors(os.path.join(self.cwd, "priors.initialized.txt"))

    @log.timed("Running iteration {self.iteration_i}+1")  # FIXME i+1
    def do_iteration(self, bam_filename, reference_fasta_filename):
        """
        This starts with the M-step, so it requires that Pr(S) and Pr(N=n) from previous round are set.
        Pr(S) is used from the previous round's E-step.
        Pr(N=n) partially depends on the previous round's M-step.
        Once M-step is done, then E-step calculates Pr(S) based upon the just-calculated M-step.
        """
        self.iteration_i += 1

        self.iterdir = os.path.join(self.cwd, "%s%02d" % (self.iterdir_prefix, self.iteration_i))
        check_call("mkdir -p %s" % (self.iterdir), shell=True)
        self.read_bam(bam_filename, reference_fasta_filename)  # initializes all data structures.

        # m-step
        self.calc_likelihoods()
        self.calc_posteriors()

        # now e-step
        self.calc_priors()

        # now write a new fasta file.  Cull sequences below self.min_depth
        consensus_filename = os.path.join(self.iterdir, "iter.%02d.cons.fasta" % (self.iteration_i))
        self.write_consensus(consensus_filename)  # culls and splits
        # if self.iteration_i == self.max_iterations:
        self.cluster_sequences(consensus_filename)  # merges sequences that have evolved to be the same (VSEARCH)

        # leave a few things around for later.  Note that print_priors also leaves sequence_name2sequence_i mapping, basically.
        self.print_priors()
        self.print_probN()

        # delete bamfile from previous round (keep -- and convert to
        # compressed bam -- initial iteration mapping in the
        # background)
        if self.iteration_i == 0 and self.current_bam_filename.endswith(".u.bam"):  # initial iteration
            renamed = self.current_bam_filename.rstrip(".u.bam") + ".bam"
            # self.initial_compress_process = Popen(["samtools", "view", "-h", "-b", self.current_bam_filename, "-o", renamed], stdout = sys.stdout, stderr = sys.stderr)  # child process runs in background
            self.initial_compress_process = Popen("samtools view -h -b %s > %s" % (self.current_bam_filename, renamed),
                                                  shell=True, stderr=sys.stderr)  # child process runs in background

            self.initial_bam_filename_to_remove = self.current_bam_filename
        if self.iteration_i >= 1:
            os.remove(self.current_bam_filename)
            # check up on initial mapping compression background process once per iteration here
            if self.initial_compress_process is not None:
                poll = self.initial_compress_process.poll()
                if poll == 0:  # completed successfully
                    os.remove(self.initial_bam_filename_to_remove)
                    self.initial_compress_process = None  # don't bother in future
                elif poll is None:
                    if self.iteration_i == self.max_iterations - 1:  # shouldn't happen... but to be correct
                        print >> sys.stderr, "Waiting for initial bamfile to compress before finishing...",
                        self.initial_compress_process.wait()
                        print >> sys.stderr, "DONE"
                    else:
                        pass
                else:  # poll() returned something bad.
                    log.warning("Failed to compress initial mapping bamfile.\n"
                                "Failure with exit code: %s.\n"
                                "File remains uncompressed: %s"
                                % (poll, self.initial_bam_filename_to_remove))
                    self.initial_compress_process = None  # don't bother in future

        # now do a new mapping run for next iteration
        self.do_mapping(consensus_filename, nice=self.mapping_nice)

    @log.timed("Writing priors for iteration {self.iteration_i}")
    def print_priors(self, ofname=None):
        """
        leave a file in directory with nonzero priors printed out.
        """
        if ofname is not None:
            of = file(ofname, 'w')
        else:
            of = file(os.path.join(self.iterdir, "priors.iter.%02d.txt" % (self.iteration_i)), 'w')
        sequence_i2sequence_name_array = numpy.array(self.sequence_i2sequence_name)  # faster slicing?
        for seq_i, prior in enumerate(self.priors[-1]):
            seqname = sequence_i2sequence_name_array[seq_i]
            of.write("%d\t%s\t%.10f\n" % (seq_i, seqname, prior))

        of.close()

    @log.timed("Writing probN for iteration {self.iteration_i}")
    def print_probN(self):
        # python gzip.GzipFile is slow.  Use system call to gzip instead
        pickled_filename = os.path.join(self.iterdir, 'probN.pkl')
        cPickle.dump(self.probN, file(pickled_filename, 'w'), cPickle.HIGHEST_PROTOCOL)
        check_call("gzip -f %s" % (pickled_filename), shell=True, stdout=sys.stdout, stderr=sys.stderr)

    def calc_priors(self):
        """
        calculates priors [ Pr(S) ] based on
            Pr(S|R) (current posteriors from previous M step, this iteration)
        """
        # here we do have column summing with the posteriors
        # therefore, should be csc sparse type for efficient summing
        self.posteriors[-1] = self.posteriors[-1].tocsc()
        self.priors[-1] = numpy.asarray(self.posteriors[-1].sum(axis=1)).flatten() / self.posteriors[-1].sum()

    @log.timed("Writing consensus for iteration {self.iteration_i}")
    def write_consensus(self, outputfilename):
        """
        writes a consensus, taking the most probable base at each position, according to current
        values in Pr(N=n) (self.probN)

        only write sequences with coverage above self.min_depth (culling)
        split sequences with many minor alleles:
             self.snp_minor_prob_thresh     # if prob(N) for minor allele base N is >= this threshold, call site a minor allele
             self.snp_percentage_thresh     # if >= this percentage of bases are minor alleles (according to self.snp_minor_prob_thresh),
                                            # then split this sequence into two sequences.

        """
        log.info("\tsnp_minor_prob_thresh = %.3f" % (self.snp_minor_prob_thresh))
        log.info("\tsnp_percentage_thresh = %.3f" % (self.snp_percentage_thresh))

        splitcount = 0
        cullcount = 0
        of = file(outputfilename, 'w')

        times_split   = []              # DEBUG
        times_posteriors   = []              # DEBUG
        seqs_to_process = len(self.probN) # DEBUG
        print "seqs to process = %s"%seqs_to_process

        i2base = self.i2base
        rows_to_add = []                # these are for updating posteriors at end with new minor strains
        cols_to_add = []
        data_to_add = []
        probNtoadd  = []  # for newly split out sequences

        self.posteriors[-1] = self.posteriors[-1].tolil()  # just to make sure this is in row-access-friendly format

        loop_t0 = time()
        for seq_i in range(len(self.probN)):
            seq_i_t0 = time()
            if self.probN[seq_i] is None: # means this sequence is no longer present in this iteration or was culled in reset_probN
                continue
            # FOLLOWING CULLING RULES REMOVED in favor of length-coverage culling in reset_probN()
            # check if coverage passes self.min_depth, if not don't write it (culling happens here)
            # if self.min_depth is not None and self.coverage[seq_i] < self.min_depth: #  and self.iteration_i > 5:
            #     # could adjust priors and posteriors here, but because
            #     # prior will already be low (b/c of low coverage) and
            #     # because next round will have 0 mappings (no sequence
            #     # in reference file to map to), this seems
            #     # unneccesary.

            #     # probNarray = None  # NOT PASSED BY REF, assignment is only local?
            #     self.probN[seq_i] = None
            #     cullcount += 1
            #     continue # continue == don't write it to consensus.

            # else passes culling thresholds
            title = self.sequence_i2sequence_name[seq_i]
            consensus = numpy.array([i2base.get(x, "N") for x in numpy.argsort(self.probN[seq_i])[:, -1]])

            ## check for deletion, collect deletion sites:
            #deletion_threshold  = self.indel_thresh
            #prob_indels_single = self.prob_indels[seq_i] #retreive single numpy matrix of prob_indel values for seq_i from prob_indels list of numpy matrices
            #del_hits=[]
            #for base_i in range (prob_indels_single.shape[0]):
            ## Eval if deletion exists at base position
            ## Divides weight of deletion, by the sum of both deletions and matches
            #    denominator = (prob_indels_single[base_i,0] + prob_indels_single[base_i,2])
            #    if denominator < 0:
            #        raise ValueError, "denominator should never be < 0 (actual: %s)"%denominator
            #    else:
            #        if (prob_indels_single[base_i,2] / denominator) > deletion_threshold:
            #            del_hits.append(base_i)
            # deletion_indices=numpy.array(del_hits)

            # check for minor allele consensus, SPLIT sequence into two candidate sequences if passes thresholds.
            minor_indices = numpy.argwhere((self.probN[seq_i] >= self.snp_minor_prob_thresh).sum(axis=1) >= 2)[:, 0]
            if minor_indices.shape[0] > 0:
                minor_fraction_avg = numpy.mean(self.probN[seq_i][(minor_indices, numpy.argsort(self.probN[seq_i][minor_indices])[:, -2])])
            else:
                minor_fraction_avg = 0.0
            # NEW rule: only split sequence if *expected* coverage
            # of newly split minor sequence (assuming uniform read
            # coverage over reconstructed sequence) is > some
            # threshold.  Here, expected coverage is calculated
            # based on:
            # Prior(seq_i) * number of MAPPED reads * avg read length * 2 seq per pair
            expected_coverage_minor = ( self.priors[-1][seq_i] * minor_fraction_avg * self.n_reads_mapped * self.mean_read_length ) / self.probN[seq_i].shape[0]
            expected_coverage_major = ( self.priors[-1][seq_i] * (1-minor_fraction_avg) * self.n_reads_mapped * self.mean_read_length ) / self.probN[seq_i].shape[0]

            if self.reads2_filepath is not None:  # multipy by 2 because n_reads_mapped is actually number of mapped pairs
                expected_coverage_minor *= 2.0
                expected_coverage_major *= 2.0

            # Deletions will override minor indices, so remove conflicting indices from minor_indices array:
            # if deletion_indices.shape[0] > 0:
            #    mi=[]
            #    for i in minor_indices:
            #            if i not in deletion_indices:
            #                mi.append(i)
            #    minor_indices=numpy.array(mi)

            #if (deletion_indices.shape[0] + minor_indices.shape[0]) / float(self.probN[seq_i].shape[0]) >= self.snp_percentage_thresh and \
            #       expected_coverage_minor >= self.expected_coverage_thresh:
            if minor_indices.shape[0] / float(self.probN[seq_i].shape[0]) >= self.snp_percentage_thresh and \
                   expected_coverage_minor >= self.expected_coverage_thresh:
                # We split!
                splitcount += 1
                if self._VERBOSE:
                    t0_split = time()
                major_fraction_avg = 1.-minor_fraction_avg # if there's >=3 alleles, major allele keeps prob of other minors)
                minor_bases   = numpy.array([i2base.get(x, "N") for x in numpy.argsort(self.probN[seq_i][minor_indices])[:,-2]]) # -2 gets second most probable base
                minor_consensus = consensus.copy()               # get a copy of the consensus
                minor_consensus[minor_indices] = minor_bases     # replace the bases that pass minor threshold

                #remove deletion bases from minor_consensus - deletions form part of this new sequence
                #new_minor_c=[]
                #for i in range(minor_consensus.shape[0]):
                #    if not i in deletion_indices:
                #        new_minor_c.append(minor_consensus[i])
                # new_minor_c = numpy.array(new_minor_c)

                # now deal with naming.
                title_root = re.search(r'(.+)(_m(\d+))$', title)
                if title_root is None:  # no _m00 on this name
                    title_root = title[:]
                else:
                    title_root = title_root.groups()[0]
                # now check for any known name with same root and a _m on it.
                previous_m_max = max([0] + [int(x) for x in re.findall(r'%s_m(\d+)'%re.escape(title_root), " ".join(self.sequence_i2sequence_name))])
                m_title = "%s_m%02d"%(title_root, previous_m_max+1)

                # also split out Priors and Posteriors (which will be used in next round), split with average ratio of major to minor alleles.
                # updating priors first:
                old_prior = self.priors[-1][seq_i]
                self.priors[-1][seq_i] = old_prior * major_fraction_avg
                seq_i_minor = self.n_sequences
                self.n_sequences += 1
                self.sequence_i2sequence_name.append(m_title)
                assert len(self.sequence_i2sequence_name) == self.n_sequences
                assert len(self.sequence_i2sequence_name) == seq_i_minor + 1
                self.sequence_name2sequence_i[m_title] = seq_i_minor
                self.split_seq_first_appeared[seq_i] = self.iteration_i
                # how I adjust probN here for newly split seq doesn't really matter,
                # as it is re-calculated next iter.
                # this only matters for probN.pkl.gz file left behind for this iteration.
                # for now just set prob(major base) = 0 and redistribute prob to other bases for minor,
                # and set prob(minor base) = 0 and redistribute prob to other bases for major
                # MINOR
                major_base_i = numpy.argsort(self.probN[seq_i][minor_indices])[:, -1]
                newprobNarray = self.probN[seq_i].copy()
                newprobNarray[(minor_indices, major_base_i)] = 0
                #if deletion_indices.shape[0]> 0:
                 #   deletion_base_i = numpy.argsort(self.probN[seq_i][deletion_indices])[:,-1]
                  #  newprobNarray[(deletion_indices,deletion_base_i)] = 0  #set major to 0 for deletion base?
                newprobNarray = newprobNarray / numpy.sum(newprobNarray, axis=1).reshape(newprobNarray.shape[0], 1)
                probNtoadd.append(newprobNarray)
                self.base_coverages.append(numpy.zeros_like(self.base_coverages[seq_i])) #why are we doing this?!?!?

                # MAJOR
                minor_base_i = numpy.argsort(self.probN[seq_i][minor_indices])[:, -2]
                self.probN[seq_i][(minor_indices, minor_base_i)] = 0
                self.probN[seq_i] = self.probN[seq_i] / numpy.sum(self.probN[seq_i], axis=1).reshape(self.probN[seq_i].shape[0], 1)

                new_priors = numpy.zeros(seq_i_minor + 1, dtype=self.priors[-1].dtype)
                new_priors[:-1] = self.priors[-1].copy()
                new_priors[seq_i_minor] = old_prior * minor_fraction_avg
                trash = self.priors.pop()
                del trash
                self.priors.append(new_priors)

                # keep track of all new minor data to add and add it
                # once at end for ALL split sequences with one coo
                # matrix construction, instead of each iteration.

                t_posterior = time()
                # new_read_probs, new_rows, new_cols = adjust_posteriors_for_split(AAAA, BBBB, CCCC) # TODO: could move to Cython
                # updating posteriors. for each seq-read pair with prob > 0, split prob out to major and minor seq.
                new_cols = self.posteriors[-1].rows[seq_i] # col in coo format
                new_read_probs  = [x * minor_fraction_avg for x in self.posteriors[-1].data[seq_i]]  # data in coo format
                new_rows = [seq_i_minor for x in new_cols]  # row in coo format

                # add new read probs to cache of new read probs to add at end of loop
                rows_to_add.extend(new_rows)
                cols_to_add.extend(new_cols)
                data_to_add.extend(new_read_probs)

                # adjust old read probs to reflect major strain
                self.posteriors[-1].data[seq_i] = [x * major_fraction_avg for x in self.posteriors[-1].data[seq_i]]
                times_posteriors.append(time() - t_posterior)

                # adjust self.unmapped_bases (used in clustering).  For now give same pattern as parent
                self.unmapped_bases.append(self.unmapped_bases[seq_i].copy())

                # write out minor strain consensus
                of.write(">%s\n" % (m_title))
                minor_consensus = self.eval_indels(seq_i, minor_consensus, m_title)  # added for indels
                of.write("%s\n" % ("".join(minor_consensus)))
                log.info("splitting sequence %d (%s) to %d (%s)...\n"
                         % (seq_i, title, seq_i_minor, m_title))
                times_split.append(time() - seq_i_t0)

            # now write major strain consensus, regardless of whether there was a minor strain consensus
            of.write(">%s\n" % (title))
            consensus = self.eval_indels(seq_i, consensus, title)  # added for indels
            of.write("%s\n" % ("".join(consensus)))

        # END LOOP
        loop_t_total = time() - loop_t0
        # update posteriors matrix with newly added minor sequences new_posteriors via coo, then convert to csr.
        new_posteriors = self.posteriors[-1].tocoo()  # first make a copy in coo format
        # then create new coo matrix with new shape, appending new row, col, data to old row, col, data

        new_posteriors = sparse.coo_matrix((numpy.concatenate((new_posteriors.data, data_to_add)),
                                            (numpy.concatenate((new_posteriors.row, rows_to_add)),
                                             numpy.concatenate((new_posteriors.col, cols_to_add)))),
                                           shape=(self.n_sequences, self.posteriors[-1].shape[1]),
                                           dtype=new_posteriors.dtype).tocsr()

        # finally, exchange in this new matrix
        trash = self.posteriors.pop()
        del trash
        self.posteriors.append(new_posteriors)

        # update probN array:
        self.probN.extend(probNtoadd)

        log.info("Split out %d new minor strain sequences." % (splitcount))
        if splitcount > 0:
            log.info("Average time for split sequence: [%.6f seconds]"
                     % numpy.mean(times_split))
            log.info("Average time for posterior update: [%.6f seconds]"
                     % numpy.mean(times_posteriors))
            log.info("Average time for non-split sequences: [%.6f seconds]"
                     % ((loop_t_total - sum(times_split)) / (seqs_to_process - len(times_split))))

    def eval_indels(self, seq_i, consensus, title):
        # Evaluates consensus sequence for write outs against the prob_indels array.  deletes or inserts bases as appropriate
        # OUT:   returns a list of single character bases as new consensus
        deletion_threshold = self.indel_thresh
        insertion_threshold = self.indel_thresh
        new_cons = []
        prob_indels_single = self.prob_indels[seq_i] #retreive single numpy matrix of prob_indel values for seq_i from prob_indels list of numpy matrices
        for base_i in range (prob_indels_single.shape[0]):
            # Eval if deletion exists at base position
            # Divides weight of deletion, by the sum of both deletions and matches
            denominator = (prob_indels_single[base_i,0] + prob_indels_single[base_i,2])
            
            if denominator < 0:
                raise ValueError, "denominator should never be < 0 (actual: %s)" % denominator

            elif denominator == 0:  # nothing mapped to this base.  Use consensus base from reference
                new_cons.append(consensus[base_i])

            else:
                if consensus[base_i] != "D":
                    new_cons.append(consensus[base_i]) # not deleted
                #if (prob_indels_single[base_i,2]) == 0:  # no evidence for deletion
                    #new_cons.append(consensus[base_i]) # not deleted
                #elif (prob_indels_single[base_i,2] / denominator) < deletion_threshold: #doesn't meet criteria for deletion
                 #   new_cons.append(consensus[base_i]) # not deleted
                else:
                    # delete (add nothing to new consensus)
                    log.info("Modified reference sequence %d (%s) with a deletion of base %d "
                             % (seq_i, title, base_i))
    

            # Eval if insertion exists after base position
                if (prob_indels_single[base_i,1]) == 0:  # no evidence for insertion
                    continue
            # if summed weights of insertion is greater than sum of reads mapped nearby (at base on left flank of proposed insertion (because it's easy), i-1)
                elif (prob_indels_single[base_i,1] / denominator) > insertion_threshold: 
                    new_cons.append('N')
                    log.info("Modified reference sequence %d (%s) with a single base "
                             "insertion after base %d"
                             % (seq_i, title, base_i))
                else:  # no insertion
                    continue
  
        return new_cons

    def write_consensus_with_mask(self, reference_fastafilename, output_fastafilename, mask):
        """
        write a consensus sequence to output_fastafilename for each
        sequence in probN where unmapped bases are replaced with:
          mask == "hard"  --> N
          mask == "soft"  --> lowercase letters
        If masking with soft bases, use reference_fastafilename for bases to use for unmapped bases.
        this is useful prior to vsearch clustering.

        OUT: number of sequences processed
        """
        n_seqs = 0
        i2base_get = self.i2base.get  # for speed
        of = file(output_fastafilename, 'w')
        reference_fastafile = pysam.Fastafile(reference_fastafilename)
        
        for seq_i in range(len(self.probN)):
            if self.probN[seq_i] is None:
                continue
            title = self.sequence_i2sequence_name[seq_i]
            consensus = numpy.array([i2base_get(x, "N") for x in numpy.argsort(self.probN[seq_i])[:,-1]])
            orig_bases = numpy.array(reference_fastafile.fetch(title).lower(), dtype='c')
            # now replace consensus bases with no read support with N
            # unmapped_indices = numpy.where(self.unmapped_bases[seq_i] == 1)
            unmapped_indices = numpy.where(consensus == "N")
            if mask == "hard":
                consensus[unmapped_indices] = 'N'
            elif mask == "soft":
                for unmapped_i in unmapped_indices[0]:
                    consensus[unmapped_i] = orig_bases[unmapped_i] # return to original base if unmapped / ambiguous
                # consensus[unmapped_indices] = [letter.lower() for letter in consensus[unmapped_indices]]
            else:
                raise ValueError, "Invalid valud for mask: %s (choose one of {soft, hard}"%mask
            of.write(">%s\n"%(title))
            of.write("%s\n"%("".join(consensus)))
            n_seqs += 1
        of.close()

        return n_seqs

    def cluster_sequences(self, fastafilename):
        """
        Right now, this simply calls cluster_sequences_vsearch, which
        uses VSEARCH.  Could swap in other functions here if there
        were faster or just alternative clustering methods to try out

        called function should also adjust Pr(S) [prior] and Pr(S_t-1)
        [posteriors] as needed after merging.
        """
        return self.cluster_sequences_vsearch(fastafilename)

    @log.timed("Clustering sequences for iteration {self.iteration_i}")
    def cluster_sequences_vsearch(self, fastafilename):
        """
        uses VSEARCH to merge sequences above self.cluster_thresh %ID over the
        length of the shorter sequence

        Merge two sequences if the *NON-GAPPED* positions have %
        identity >= self.cluster_thresh

        also adjusts Pr(S) [prior] and Pr(S_t-1) [posteriors] as needed after merging.
        """
        # Hard threshold in place for EMIRGE2.  cluster_thresh from -j flag
        # is now applied only in post-processing steps
        log.info("cluster threshold = 100.00%")

        tocleanup = []  # list of temporary files to remove after done

        # get posteriors ready for slicing (just prior to this call, is csr matrix?):
        self.posteriors[-1] = self.posteriors[-1].tolil()

        # NOTE that this fasta file now contains N's where there are
        # no mapped bases, so that vsearch with iddef 0 will not count
        # positions aligned to these bases in the identity calculation

        tmp_fastafilename = fastafilename + ".tmp.fasta"
        num_seqs = self.write_consensus_with_mask(fastafilename, tmp_fastafilename, mask="soft")
        tocleanup.append(tmp_fastafilename)
        tmp_fastafile = pysam.Fastafile(tmp_fastafilename)
        tocleanup.append("%s.fai" % (tmp_fastafilename))
        # do global alignments with VSEARCH
        # I don't use --cluster because it doesn't report alignments
        # vsearch is fast but will sometimes miss things -- I've tried to tune params as best as I can.
        # and I use different parameters depending on how many input sequences there are
        # Also, I use a lower %ID thresh than specified for joining because I really calculate %ID over *mapped* sequence positions.

        # sens_string = "--maxaccepts 8 --maxrejects 256"
        # if self.iteration_i == self.max_iterations:
        #        uclust_id=0.80
        # else:
        #        uclust_id=1.0
        sens_string = "--maxaccepts 8 --maxrejects 256"
        uclust_id = 0.80
        algorithm = "-usearch_global"

        # if em.iteration_i > 10:
        # num_seqs = len([x for x in self.probN if x is not None])
        assert num_seqs == len([x for x in self.probN if x is not None])
        if num_seqs < 1000:
            sens_string = "--maxaccepts 16 --maxrejects 256"
        if num_seqs < 500:
            sens_string = "--maxaccepts 32 --maxrejects 256"
        if num_seqs < 150:
            algorithm = "-usearch_global"
            sens_string = "--maxaccepts 0 --maxrejects 0"  # slower, but more sensitive.
        # if really few seqs, then no use not doing smith-waterman or needleman wunsch alignments
        if num_seqs < 50:
            algorithm = "-usearch_global"
            sens_string = "-fulldp"

        cmd = "vsearch %s %s --db %s --id %.3f --query_cov 0.5 --target_cov 0.5 --strand plus --userout %s.us.txt --userfields query+target+id+caln+qlo+qhi+tlo+thi --threads %d %s --quiet"%\
              (algorithm,
               tmp_fastafilename, tmp_fastafilename,
               uclust_id,
               tmp_fastafilename,
               self.n_cpus,
               sens_string)

        log.info("vsearch command was:\n%s" % (cmd))

        check_call(cmd, shell=True, stdout=sys.stdout, stderr=sys.stderr)
        # read clustering file to adjust Priors and Posteriors, summing merged reference sequences
        tocleanup.append("%s.us.txt" % tmp_fastafilename)

        nummerged = 0
        alnstring_pat = re.compile(r'(\d*)([MDI])')
        already_removed = set()  # seq_ids
        # this is a bit slow and almost certainly could be sped up with algorithmic improvements.
        times = []  # DEBUG
        for row in csv.reader(file("%s.us.txt" % tmp_fastafilename), delimiter='\t'):
            # each row an alignment in userout file
            t0 = time()
            # member == query
            member_name = row[0]
            seed_name = row[1]
            if member_name == seed_name:
                continue  # vsearch allows self-hits, which we don't care about
            member_seq_id = self.sequence_name2sequence_i.get(member_name)
            seed_seq_id = self.sequence_name2sequence_i.get(seed_name)
            if member_seq_id in already_removed or seed_seq_id in already_removed:
                continue

            # decide if these pass the cluster_thresh *over non-gapped, mapped columns*
            member_fasta_seq = tmp_fastafile.fetch(member_name)
            seed_fasta_seq = tmp_fastafile.fetch(seed_name)
            member_unmapped = self.unmapped_bases[member_seq_id]  # unmapped positions (default prob)
            seed_unmapped = self.unmapped_bases[seed_seq_id]
            # query+target+id+caln+qlo+qhi+tlo+thi %s"%\
            #   0     1     2   3   4   5  6    7
            member_start = int(row[4]) - 1  # printed as 1-based by vsearch now
            seed_start = int(row[6]) - 1

            t0 = time()
            # print >> sys.stderr, "DEBUG", alnstring_pat.findall(row[3])
            aln_columns, matches = amplicon.count_cigar_aln(tmp_fastafile.fetch(seed_name),
                                                            tmp_fastafile.fetch(member_name),
                                                            self.unmapped_bases[seed_seq_id],
                                                            self.unmapped_bases[member_seq_id],
                                                            seed_start,
                                                            member_start,
                                                            alnstring_pat.findall(row[3]))
            ## print >> sys.stderr, "DEBUG: %.6e seconds"%(time()-t0)# timedelta(seconds = time()-t0)

            # if alignment is less than 1000 bases, or identity over those 500+ bases is not above thresh, then continue
            seed_n_mapped_bases = self.unmapped_bases[seed_seq_id].shape[0] - self.unmapped_bases[seed_seq_id].sum()
            member_n_mapped_bases = self.unmapped_bases[member_seq_id].shape[0] - self.unmapped_bases[member_seq_id].sum()

            
            if (aln_columns < 500) \
                   or ((float(matches) / aln_columns) < 1.0):  # Fixed merging threshold for EMIRGE2.  During iterations only merge seqs that are 100% identical. Rest are cleaned up in post-processing steps.
                    #or ((float(matches) / aln_columns) < self.cluster_thresh):
                   # or (float(aln_columns) / min(seed_n_mapped_bases, member_n_mapped_bases) < 0.9)
                continue

            minimum_residence_time = -1  # how many iters does a newly split out seq have to be around before it's allowed to merge again.  -1 to turn this off.
            member_first_appeared = self.split_seq_first_appeared.get(member_seq_id)
            if member_first_appeared is not None and self.iteration_i - member_first_appeared <= minimum_residence_time:
                continue
            seed_first_appeared = self.split_seq_first_appeared.get(seed_seq_id)
            if seed_first_appeared is not None and self.iteration_i - seed_first_appeared <= minimum_residence_time:
                continue

            if num_seqs < 50:
                log.info("\t\t%s|%s vs %s|%s %.3f over %s aligned columns"
                         "(vsearch %%ID: %s)"
                         % (member_seq_id, member_name, seed_seq_id, seed_name,
                            float(matches) / aln_columns, aln_columns, row[2]))

            # if above thresh, then first decide which sequence to keep, (one with higher prior probability).
            percent_id = (float(matches) / aln_columns) * 100.
            t0 = time()
            if self.priors[-1][seed_seq_id] > self.priors[-1][member_seq_id]:
                keep_seq_id = seed_seq_id
                remove_seq_id = member_seq_id
                keep_name = seed_name
                remove_name = member_name
            else:
                keep_seq_id = member_seq_id
                remove_seq_id = seed_seq_id
                keep_name = member_name
                remove_name = seed_name

            # merge priors (add remove_seq_id probs to keep_seq_id probs).
            self.priors[-1][keep_seq_id] += self.priors[-1][remove_seq_id]
            self.priors[-1][remove_seq_id] = 0.0

            # now merge posteriors (all removed probs from remove_seq_id go to keep_seq_id).
            # self.posteriors[-1] at this point is lil_matrix
            # some manipulations of underlying sparse matrix data structures for efficiency here.
            # 1st, do addition in csr format (fast), convert to lil format, and store result in temporary array.
            new_row = (self.posteriors[-1].getrow(keep_seq_id).tocsr() + self.posteriors[-1].getrow(remove_seq_id).tocsr()).tolil()
            # then change linked lists directly in the posteriors data structure -- this is very fast
            self.posteriors[-1].data[keep_seq_id] = new_row.data[0]
            self.posteriors[-1].rows[keep_seq_id] = new_row.rows[0]
            # these two lines remove the row from the linked list (or rather, make them empty rows), essentially setting all elements to 0
            self.posteriors[-1].rows[remove_seq_id] = []
            self.posteriors[-1].data[remove_seq_id] = []

            # set self.probN[removed] to be None -- note that this doesn't really matter, except for
            # writing out probN.pkl.gz every iteration, as probN is recalculated from bam file
            # with each iteration
            self.probN[remove_seq_id] = None

            already_removed.add(remove_seq_id)
            nummerged += 1

            times.append(time() - t0)
            log.info("\t...merging %d|%s into %d|%s (%.2f%% ID over %d columns)"
                     "in %.3f seconds"
                     % (remove_seq_id, remove_name, keep_seq_id, keep_name,
                        percent_id, aln_columns, times[-1]))

        # if len(times) and self._VERBOSE:  # DEBUG
        #     sys.stderr.write("merges: %d\n"%(len(times)))
        #     sys.stderr.write("total time for all merges: %.3f seconds\n"%(numpy.sum(times)))
        #     sys.stderr.write("average time per merge: %.3f seconds\n"%(numpy.mean(times)))
        #     sys.stderr.write("min time per merge: %.3f seconds\n"%(numpy.min(times)))
        #     sys.stderr.write("max time per merge: %.3f seconds\n"%(numpy.max(times)))

        # write new fasta file with only new sequences
        log.info("Writing new fasta file for iteration %d" % (self.iteration_i))
        tmp_fastafile.close()
        tocleanup.append("%s.fai"%(fastafilename))  # this file will change!  So must remove index file.  pysam should check timestamps of these!
        recordstrings=""
        num_seqs = 0
        for record in io.FastIterator(file(fastafilename)): # read through file again, overwriting orig file if we keep the seq
            seqname = record.title.split()[0]
            seq_id = self.sequence_name2sequence_i.get(seqname)
            if seq_id not in already_removed:
                recordstrings += str(record) # could do a better job here of actually "merging" a new consensus, rather than just keeping one or the other.
                num_seqs += 1
        outfile = file(fastafilename, 'w')
        outfile.write(recordstrings)
        outfile.close()

        # clean up.  quite important, actually, to remove old fai index files.
        for fn in tocleanup:
            os.remove(fn)

        log.info("\tremoved %d sequences after merging" % (nummerged))
        log.info("\tsequences remaining for iteration %02d: %d"
                 % (self.iteration_i, num_seqs))

    @log.timed("Mapping reads for iteration {self.iteration_i}")
    def do_mapping(self, full_fasta_path, nice=None):
        """
        IN:  path of fasta file to map reads to
        run external mapping program to produce bam file
        right now this is bowtie

        should also set self.n_alignments and self.current_bam_filename
        """

        self.do_mapping_bowtie2(full_fasta_path, nice=nice)
        # self.do_mapping_bowtie(full_fasta_path, nice = nice)

    # bowtie2 version:
    def do_mapping_bowtie2(self, full_fasta_path, nice=None):
        """
        run bowtie2 to produce bam file for next iteration

        """
        bowtie_logfile = os.path.join(self.iterdir, "bowtie.iter.%02d.log"%(self.iteration_i))
        bowtie_index   = os.path.join(self.iterdir, "bowtie.index.iter.%02d"%(self.iteration_i))
        # 1. build index
        cmd = "bowtie2-build -o 3 %s %s > %s 2>&1"%(full_fasta_path , bowtie_index, bowtie_logfile) # -o 3 for speed? magnitude of speedup untested!
        log.info("\tbowtie2-build command:")
        log.info("\t%s" % cmd)
        check_call(cmd, shell=True, stdout=sys.stdout, stderr=sys.stderr)

        # 2. run bowtie
        nicestring = ""
        if nice is not None:
            nicestring = "nice -n %d" % (nice)

        if self.reads1_filepath.endswith(".gz"):
            cat_cmd = "gzip -dc "
        else:
            cat_cmd = "cat "

    
        # minins = max((self.insert_mean - 3*self.insert_sd), self.max_read_length) # this was to keep "dovetailing" (overlapping reads that extend past each other) reads from mapping, but causes problems with modern overlapped reads common on MiSeq, etc.  Goal was to keep adapter sequence bases out of EMIRGE's view.
        minins = max((self.insert_mean - 3*self.insert_sd), 0) # Puts burden on user to make sure there are no adapter sequences in input reads.
        maxins = self.insert_mean + 3*self.insert_sd
        output_prefix = os.path.join(self.iterdir, "bowtie.iter.%02d"%(self.iteration_i))
        output_filename = "%s.PE.u.bam"%output_prefix


        # these are used for single reads too.  Bowtie2 ignores the paired-end options when single reads are used

        shared_bowtie_params = "-D 20 -R 3 -N 0 -L 20 -i S,1,0.50 -k 20 --no-unal --phred%d -t -p %s"%(\
            self.reads_ascii_offset, 
            self.n_cpus)

        #Build Bowtie2 command depending if reads2 was given in Emirge command line parameters

        if self.reads2_filepath is not None:
            bowtie_command = "%s %s | %s bowtie2 %s -I %d -X %d --no-mixed --no-discordant -x %s -1 - -2 %s | samtools view -b -S - > %s 2> %s "%(\
                cat_cmd,
                self.reads1_filepath,
                nicestring,
                shared_bowtie_params,
                minins, maxins,
                bowtie_index,
                self.reads2_filepath,
                output_filename,
                bowtie_logfile)
        else: # single reads
            bowtie_command = "%s %s | %s bowtie2 %s -x %s -U - | samtools view -b -S - > %s 2> %s "%(\
                cat_cmd,
                self.reads1_filepath,
                nicestring,
                shared_bowtie_params,
                bowtie_index,
                output_filename,
                bowtie_logfile)

        log.info("\tbowtie command:")
        log.info("\t%s" % bowtie_command)

        p = Popen(bowtie_command, shell=True, stdout=sys.stdout, stderr=PIPE, close_fds=True)
        p.wait()
        stderr_string = p.stderr.read()
        self.fragments_mapped = self.get_frags_mapped_bt2(stderr_string)
        sys.stdout.write(stderr_string)
        sys.stdout.flush()

        log.info("\tFinished Bowtie for iteration %02d" % (self.iteration_i))

        # 3. clean up
        # check_call("samtools index %s.sort.PE.bam"%(output_prefix), shell=True, stdout = sys.stdout, stderr = sys.stderr)
        check_call("gzip -f %s" % (bowtie_logfile), shell=True)

        assert self.iterdir != '/'
        for filename in os.listdir(self.iterdir):
            assert (len(os.path.basename(bowtie_index)) >= 20)  # weak check that I'm not doing anything dumb.
            if os.path.basename(bowtie_index) in filename:
                os.remove(os.path.join(self.iterdir, filename))

        self.current_bam_filename = output_filename  # do this last.


   # original bowtie1 version:   
    def do_mapping_bowtie(self, full_fasta_path, nice=None):
        """
        run bowtie to produce bam file for next iteration

        sets self.n_alignments
        sets self.current_bam_filename
        """
        bowtie_index   = os.path.join(self.iterdir, "bowtie.index.iter.%02d"%(self.iteration_i))
        bowtie_logfile = os.path.join(self.iterdir, "bowtie.iter.%02d.log"%(self.iteration_i))
        # 1. build index
        cmd = "bowtie-build -o 3 %s %s > %s"%(full_fasta_path , bowtie_index, bowtie_logfile) # -o 3 for speed? magnitude of speedup untested!
        # note: just send stdout to log file, as stderr captured in emirge stderr
        log.info("\tbowtie-build command:")
        log.info("\t%s" % cmd)
        check_call(cmd, shell=True, stdout=sys.stdout, stderr=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()

        # 2. run bowtie
        nicestring = ""
        if nice is not None:
            nicestring = "nice -n %d" % (nice)

        if self.reads1_filepath.endswith(".gz"):
            cat_cmd = "gzip -dc "
        else:
            cat_cmd = "cat "

        # these are used for single reads too.
        shared_bowtie_params = "--phred%d-quals -t -p %s  -n 3 -l %s -e %s  --best --strata --all --sam --chunkmbs 512"%(self.reads_ascii_offset, self.n_cpus, BOWTIE_l, BOWTIE_e)

        # minins = max((self.insert_mean - 3*self.insert_sd), self.max_read_length) # this was to keep "dovetailing" (overlapping reads that extend past each other) reads from mapping, but causes problems with modern overlapped reads common on MiSeq, etc.  Goal was to keep adapter sequence bases out of EMIRGE's view.
        minins = max((self.insert_mean - 3*self.insert_sd), 0) # Puts burden on user to make sure there are no adapter sequences in input reads. 
        maxins = self.insert_mean + 3*self.insert_sd
        output_prefix = os.path.join(self.iterdir, "bowtie.iter.%02d"%(self.iteration_i))
        output_filename = "%s.PE.u.bam"%output_prefix
        samtools_cmd    = "samtools view -S -h -u -b -F 0x0004 -"  # -F instead of piping to awk?    |  awk '{if ($3!="*") print }'

        if self.reads2_filepath is not None:
            bowtie_command = """%s %s | %s bowtie %s --minins %d --maxins %d %s -1 - -2 %s | %s > %s"""%(\
                cat_cmd,
                self.reads1_filepath,
                nicestring,
                shared_bowtie_params,
                minins, maxins,
                bowtie_index,
                self.reads2_filepath,
                samtools_cmd,
                output_filename)
        else:  # single reads
            bowtie_command = """%s %s | %s bowtie %s %s - | %s > %s""" % ( \
                cat_cmd,
                self.reads1_filepath,
                nicestring,
                shared_bowtie_params,
                bowtie_index,
                samtools_cmd,
                output_filename)

        log.info("\tbowtie command:")
        log.info("\t%s" % bowtie_command)

        p = Popen(bowtie_command, shell=True, stdout=sys.stdout, stderr=PIPE, close_fds=True)
        p.wait()
        stderr_string = p.stderr.read()
        self.fragments_mapped = self.get_frags_mapped_bt1(stderr_string)
  

        # re-print this to stdout, since we stole it from bowtie
        sys.stdout.write(stderr_string)
        sys.stdout.flush()
        # and now put in separate bowtie logfile
        of = open(bowtie_logfile, 'w')
        of.write("\nBOWTIE STDERR:\n")
        of.write(stderr_string)
        of.write("\n")
        of.close()

        log.info("\tFinished Bowtie for iteration %02d" % (self.iteration_i))

        # 3. clean up
        # check_call("samtools index %s.sort.PE.bam"%(output_prefix), shell=True, stdout = sys.stdout, stderr = sys.stderr)
        if os.path.exists(bowtie_logfile):
            check_call("gzip -f %s" % (bowtie_logfile), shell=True)

        assert self.iterdir != '/'
        for filename in os.listdir(self.iterdir):
            assert (len(os.path.basename(bowtie_index)) >= 20)  # weak check that I'm not doing anything dumb.
            if os.path.basename(bowtie_index) in filename:
                os.remove(os.path.join(self.iterdir, filename))

        self.current_bam_filename = output_filename  # do this last.

    def get_n_alignments_from_bowtie(self):
        """
        bowtie2 output has changed,
        use this to get number of aligned reads
        """
        bam_to_use = self.current_bam_filename
        cmd = "samtools view -c %s " % (bam_to_use)
        p = Popen(cmd, shell=True, stdout=PIPE, stderr=sys.stderr, close_fds=True)
        stdout_string = p.stdout.read()
        return int(stdout_string)

    def get_frags_mapped_bt1(self, stderr_string):
        try:
            r = re.findall(r'Reported ([0-9]+) (paired-end )?alignments', stderr_string)
            if r[0][1] != '':  # "paired-end" string matched -- two lines in samfile per paired-end aln
                return int(r[0][0]) * 2
            else:  # single-end -- one line in samfile per alignment
                return int(r[0][0])
        except IndexError:
            log.error("OOPS, we didn't get number of reads from bowtie:")
            log.error(stderr_string)
            log.error(r)
            raise

    def get_frags_mapped_bt2(self, stderr_string):
        """
        49227 reads; of these:
            49227 (100.00%) were paired; of these:
                0 (0.00%) aligned concordantly 0 times
                36694 (74.54%) aligned concordantly exactly 1 time
                12533 (25.46%) aligned concordantly >1 times
        100.00% overall alignment rate
"""
        try:
            r = re.findall(r'([0-9]+) reads;', stderr_string)
            rt = re.findall(r'([0-9]+\.[0-9]+)% overall', stderr_string)
            frags = (float(rt[0]) / 100) * int(r[0])
            return int(frags)
        except IndexError:
            log.error("OOPS, we didn't get number of reads from bowtie:")
            log.error(stderr_string)
            log.error(r)
            raise

    @log.timed("Calculating likelihood {self.likelihoods.shape} "
               "for iteration {self.iteration_i}")
    def calc_likelihoods(self):
        """
        sets self.likelihoods  (seq_n x read_n) for this round
        """
        # first calculate self.probN from mapped reads, previous round's posteriors
        self.calc_probN()  # (handles initial iteration differently within this method)

        # Cython function for heavy lifting.
        amplicon.calc_likelihood(self)

    @log.timed("Calculating Pr(N=n) for iteration {self.iteration_i}")
    def calc_probN(self):
        """
        Pr(N=n)

        If read or sequence is new this round (not seen at t-1), then
        there is no Pr(S|R) from previous round, so we substitute
        Pr(S), the unbiased prior

        If initial iteration, all reads and seqs are new, so all calcs
        for Pr(N=n) use the prior as weighting factor instead of
        previous round's posterior.
        """

        # here do looping in Cython (this loop is about 95% of the time in this method on test data):
        amplicon.calc_probN(self)

    @log.timed("Calculating posteriors for iteration {self.iteration_i}")
    def calc_posteriors(self):
        amplicon.calc_posteriors(self)

    def iterations_done(self):
        """
        check if we are done iterating, i.e. are the current reference sequences the same as that from the last round

        returns True or False
        """

        return False

    def calc_cutoff_threshold(self, max_reads_to_sample=100000):  # done at the end of the final iteration
        """
        calculate the minimum abundance that will represent average coverage specified in cov_thresh (default will be 20X)
        this value is used in post-processing steps to filter final EMIRGE fasta file
        """
        self.final_iterdir = os.path.join(self.cwd, "%s%02d"%(self.iterdir_prefix, self.max_iterations))
        bam_filename = [fn for fn in os.listdir(self.iterdir) if fn.endswith('.bam') and fn.startswith("bowtie.iter.%02d"%self.max_iterations)]
        assert len(bam_filename) == 1, "ERROR: more than one valid bam file found in %s"%(self.final_iterdir)
	bam_filename = os.path.join(self.final_iterdir, bam_filename[0])
        bamfile = pysam.Samfile(bam_filename, "rb")
        self.avg_emirge_seq_length = numpy.mean(bamfile.lengths)
        # self.mean_read_length = numpy.mean(self.readlengths)

        log.warning("Average read length is %.4d"
                    % self.mean_read_length)
        log.warning("Average EMIRGE sequence length is %.5d"
                    % self.avg_emirge_seq_length)
        log.warning("Fragments mapped = %.9d"
                    % self.fragments_mapped)
        self.prob_min = (self.avg_emirge_seq_length*float(self.cov_thresh)) / (self.fragments_mapped*((int(self.paired_end)+1)*self.mean_read_length))


def do_iterations(em, max_iter, save_every):
    """
    an EM object is passed in, so that one could in theory start from a saved state
    this should be moved into the EM object.
    """
    bamfile_template = "bowtie.iter.%02d.PE.u.bam"
    os.chdir(em.cwd)

    em.max_iterations = max_iter

    if em.iteration_i < 0:  # first run
        em.do_iteration(em.current_bam_filename, em.current_reference_fasta_filename)

    while em.iteration_i < max_iter:
        subdir = os.path.join(em.cwd, "iter.%02d" % (em.iteration_i))
        em.do_iteration(os.path.join(subdir, bamfile_template % (em.iteration_i)),
                        os.path.join(subdir, "iter.%02d.cons.fasta" % (em.iteration_i)))
        # currently broken.  Not sure anyone was using this anyway
        # if em.iteration_i > 0 and (em.iteration_i % save_every == 0):
        #     filename = em.save_state()
        #     os.system("bzip2 -f %s &"%(filename))

    em.calc_cutoff_threshold()
    
    # clean up any global temporary files, i.e. rewritten reads files
    for filename in em.temporary_files:
        os.remove(filename)

    # compress last mapping (which we keep around)
    if os.path.exists(em.current_bam_filename) and em.current_bam_filename.endswith(".u.bam"):
        logthis = log.warningTimed(
            "Converting last mapping file (%s) to compressed bam"
            % (os.path.basename(em.current_bam_filename)))
        new_fn = em.current_bam_filename.rstrip(".u.sam") + ".bam"
        # p = Popen(["samtools", "view", "-h", "-b", em.current_bam_filename, "-o", new_fn], stdout = sys.stdout, stderr = sys.stderr)
        p = Popen("samtools view -h -b %s > %s"%(em.current_bam_filename, new_fn), shell=True, stderr = sys.stderr)
        returncode = p.wait()
        if returncode == 0:
            logthis.done()
            os.remove(em.current_bam_filename)
            em.current_bam_filename = new_fn
        else:
            logthis.failed("Could not convert last mapping file (%s) to compressed bam"
                           % (os.path.basename(em.current_bam_filename)))


def do_premapping(pre_mapping_dir, options):
    """
    Do only if metagenomic reads
    IN: the metagenomic reads
    OUT: a new set of reads to be used in the iterations
    """

    if not os.path.exists(pre_mapping_dir):
        os.mkdir(pre_mapping_dir)

    nicestring = ""
    if options.nice_mapping is not None:
        nicestring = "nice -n %d"%(options.nice_mapping)  # TODO: fix this so it isn't such a hack and will work in non-bash shells.  Need to rewrite all subprocess code, really (s$

    reads_ascii_offset = {False: 64, True: 33}[options.phred33]

    premapSam = os.path.join(pre_mapping_dir, "bowtie_pre_mapping.PE.sam")
    premap_reads_1 = os.path.join(pre_mapping_dir, "pre_mapped_reads.1.fastq")
    premap_reads_2 = os.path.join(pre_mapping_dir, "pre_mapped_reads.2.fastq")

    if options.fastq_reads_1.endswith(".gz"):
        option_strings = ["gzip -dc "]
    else:
        option_strings = ["cat "]


    # premapping done as single reads regardless of whether paired mapping or not  CAN'T DEAL W/GZIPPED READ 2 HERE
    if options.fastq_reads_2 is not None:
        option_strings.extend([options.fastq_reads_1,options.fastq_reads_2,nicestring, reads_ascii_offset, options.processors,options.bowtie2_db,premapSam])
        cmd = """%s %s %s | %s bowtie2 --very-sensitive-local --phred%d -t -p %s -k1 -x %s --no-unal -S %s -U - """ %tuple(option_strings)
       
    log.warning("Performing pre mapping with command:\n%s" % cmd)
    p = Popen(cmd, shell=True, stdout=sys.stdout, stderr=PIPE, close_fds=True)
    p.wait()
    stderr_string = p.stderr.read()
    sys.stdout.write(stderr_string)
    sys.stdout.flush()
    

    # get IDs of mapped reads    
    sam = pysam.AlignmentFile(premapSam, 'r')
    keepers = set()
    for read in sam.fetch():
        keepers.add(read.query_name)

    ## use IDs to create new fastq files of reads where at least one of the pair mapped in the premapping step
    r1_in = (options.fastq_reads_1)
    r2_in = (options.fastq_reads_2)
    p1_out = open(premap_reads_1, 'w')
    p2_out = open(premap_reads_2, 'w')

    # this code from fastq_pull_sequence:
    for infile, outfile in [(r1_in, p1_out), (r2_in, p2_out)]:
        ks = Kseq(infile)
        keptseqs = 0
        total_seqs = 0
        while 1:
            t = ks.read_sequence_and_quals()  # (name, sequence, qualstring)
            if t is None:
                break

            # get name up to first whitespace, check if in file
            if t[0].split()[0] in keepers:
                outfile.write("@%s\n%s\n+\n%s\n" % (t[0], t[1], t[2]))
                keptseqs += 1
            total_seqs += 1

    log.warning("Total records read: %s" % (total_seqs))
    log.warning("Total premapped records written: %s" % (keptseqs))


# bowtie2 version:
def do_initial_mapping_bt2(em, working_dir, options):
    """
#    IN:  takes the working directory and an OptionParser options object
#   
#    does the initial 1-reference-per-read bowtie mapping to initialize the algorithm
#    OUT:  path to the bam file from this initial mapping
#    """
    initial_mapping_dir = os.path.join(working_dir, "initial_mapping")
    if not os.path.exists(initial_mapping_dir):
        os.mkdir(initial_mapping_dir)

    minins = max((options.insert_mean - 3 * options.insert_stddev), options.max_read_length)
    maxins = options.insert_mean + 3 * options.insert_stddev
    bampath_prefix = os.path.join(initial_mapping_dir, "initial_bowtie_mapping.PE")

    nicestring = ""
    if options.nice_mapping is not None:
        nicestring = "nice -n %d"%(options.nice_mapping)  # TODO: fix this so it isn't such a hack and will work in bash.  Need to rewrite all subprocess code, really (shell=False)
    reads_ascii_offset = {False: 64, True: 33}[options.phred33]
    if options.fastq_reads_1.endswith(".gz"):
        option_strings = ["gzip -dc "]
    else:
        option_strings = ["cat "]
    # shared regardless of whether paired mapping or not
    option_strings.extend([options.fastq_reads_1, nicestring, reads_ascii_offset, options.processors])

    # PAIRED END MAPPING
    if options.fastq_reads_2 is not None:
        option_strings.extend([minins, maxins, options.bowtie2_db, options.fastq_reads_2, bampath_prefix])
        cmd = "%s %s | %s bowtie2 -D 20 -R 3 -N 0 -L 20 -i S,1,0.50 --no-unal --phred%d -t -p %s -I %d -X %d --no-mixed --no-discordant -x %s -1 - -2 %s | samtools view -b -S -u - > %s.u.bam "%tuple(option_strings)    
    # SINGLE END MAPPING
    else:
        option_strings.extend([options.bowtie2_db, bampath_prefix])
        cmd = "%s %s | %s bowtie2 -D 20 -R 3 -N 0 -L 20 -i S,1,0.50 --no-unal --phred%d -t -p %s -x %s -U - | samtools view -b -S -u - > %s.u.bam "%tuple(option_strings)    


    log.warning("Performing initial mapping with command:\n%s" % cmd)
    p = Popen(cmd, shell=True, stdout=sys.stdout, stderr=PIPE, close_fds=True)
    p.wait()
    stderr_string = p.stderr.read()
    em.fragments_mapped = em.get_frags_mapped_bt2(stderr_string)
    # re-print this to stdout, since we stole it.
    sys.stdout.write(stderr_string)
    sys.stdout.flush()

    return bampath_prefix + ".u.bam"


    # original bowtie1 version:
def do_initial_mapping(em, working_dir, options):
    
    """
    IN:  takes the em object, working directory and an OptionParser options object

    does the initial 1-reference-per-read bowtie mapping to initialize the algorithm
    OUT:  path to the bam file from this initial mapping

    TODO:  move this to em method.  A bit clumsy right now.
    """
    initial_mapping_dir = os.path.join(working_dir, "initial_mapping")
    if not os.path.exists(initial_mapping_dir):
        os.mkdir(initial_mapping_dir)

    # minins = max((options.insert_mean - 3*options.insert_stddev), options.max_read_length) # see comments above.  This assumes there might be adapter sequence in dovetailed reads
    minins = max((options.insert_mean - 3*options.insert_stddev), 0) # Puts burden on user to make sure there are no adapter sequences in input reads. 
    maxins = options.insert_mean + 3*options.insert_stddev
    bampath_prefix = os.path.join(initial_mapping_dir, "initial_bowtie_mapping.PE")

    nicestring = ""
    if options.nice_mapping is not None:
        nicestring = "nice -n %d"%(options.nice_mapping)  # TODO: fix this so it isn't such a hack and will work in non-bash shells.  Need to rewrite all subprocess code, really (shell=False)
    reads_ascii_offset = {False: 64, True: 33}[options.phred33]
    if options.fastq_reads_1.endswith(".gz"):
        option_strings = ["gzip -dc "]
    else:
        option_strings = ["cat "]
    # shared regardless of whether paired mapping or not
    option_strings.extend([options.fastq_reads_1, nicestring, reads_ascii_offset, options.processors, BOWTIE_l, BOWTIE_e])
    samtools_cmd    = "samtools view -S -h -u -b -F 0x0004 -"  # -F instead of piping to awk?    |  awk '{if ($3!="*") print }'

    # PAIRED END MAPPING
    if options.fastq_reads_2 is not None:
        option_strings.extend([minins, maxins, options.bowtie_db, options.fastq_reads_2, samtools_cmd, bampath_prefix])
        cmd = """%s %s | %s bowtie --phred%d-quals -t -p %s -n 3 -l %s -e %s --best --sam --chunkmbs 512 --minins %s --maxins %s %s -1 - -2 %s | %s > %s.u.bam """%tuple(option_strings)
    # SINGLE END MAPPING
    else:
        option_strings.extend([options.bowtie_db, samtools_cmd, bampath_prefix])
        cmd = """%s %s | %s bowtie --phred%d-quals -t -p %s -n 3 -l %s -e %s --best --sam --chunkmbs 512  %s - | %s > %s.u.bam """%tuple(option_strings)

    
    log.warning("Performing initial mapping with command:\n%s" % cmd)
    p = Popen(cmd, shell=True, stdout=sys.stdout, stderr=PIPE, close_fds=True)
    p.wait()
    stderr_string = p.stderr.read()
    em.fragments_mapped = em.get_frags_mapped_bt1(stderr_string)
    # re-print this to stdout, since we stole it.
    sys.stdout.write(stderr_string)
    sys.stdout.flush()

    return bampath_prefix + ".u.bam"


def resume(working_dir, options):
    """
    resume from a previous run.

    Takes the emirge working dir, and an OptionParser options object
    """
    raise NotImplementedError, "This option is currently broken, and will be fixed in a later version."
    em = EM("", "", 0, 0)  # reads1_filepath, reads2_filepath, insert_mean, insert_sd
    data_path = os.path.join(working_dir, "iter.%02d"%(options.resume_from), 'em.%02i.data.pkl.bz2'%(options.resume_from))
    log.warning("Loading saved state from %s..." % data_path)
    em.load_state(data_path)
    log.warning("Done.")

    # if the current working dir has been copied or moved, the old state will no longer be valid,
    # so need to set this again:
    em.cwd = working_dir

    # secretly (not advertised) options allowed to change in a resume
    if options.fastq_reads_1 is not None:
        em.reads1_filepath = os.path.abspath(options.fastq_reads_1)
    if options.fastq_reads_2 is not None:
        em.reads2_filepath = os.path.abspath(options.fastq_reads_2)

    # now process any *relevant* options:
    # this is broken right now because it just reverts all to defaults.
    # if options.processors is not None:
    #     em.n_cpus = options.processors
    # if options.snp_fraction_thresh is not None:
    #     em.snp_percentage_thresh = options.snp_fraction_thresh
    # if options.variant_fraction_thresh is not None:
    #     em.snp_minor_prob_thresh = options.variant_fraction_thresh
    # if options.join_threshold is not None:
    #     em.cluster_thresh = options.join_threshold
    # if options.min_depth is not None:
    #     em.min_depth = options.min_depth
    # if options.nice_mapping is not None:
    #     em.mapping_nice = options.nice_mapping

    do_iterations(em, max_iter=options.iterations, save_every=options.save_every)


def post_process(em, working_dir):
    """ Do all the postprocessing for the EMIRGE run, producing a fasta file of all EMIRGE sequences produced that meet the estimated abundance threshold to achieve an
    estimated average coverage of 20X (default) or other user specified value.  Also produces a raw file of all EMIRGE sequences produced, but not recommended to be used
    in later analyses.  Final clustering is performed at 97% ID (default) but can be changed with the '-j' flag. 
    """

    #first need to do rename(em) with no probmin - keeping all sequences for clustering and writing out the raw output file
    nomin_fasta_filename = os.path.join(working_dir, em.output_files_prefix+"_nomin_iter."'%02d'%em.max_iterations +".RAW.fasta")
    if os.path.exists(nomin_fasta_filename):
        log.warning("WARNING: overwriting file %s" % nomin_fasta_filename)
    #nomin_file_handle = open(nomin_fasta_filename, 'w')
    rename(em.final_iterdir,nomin_fasta_filename,  em.output_files_prefix, prob_min=None,no_N = False, no_trim_N = True)

    
    #next need to cluster using vsearch at the user specified threshold, default =0.97
    centroids=os.path.join(working_dir,"centroids.tmp")
    uc=os.path.join(working_dir,"uc.tmp")
    cmd = "vsearch --cluster_smallmem %s -usersort -notrunclabels -id %.2f -centroids %s -uc %s "%(nomin_fasta_filename,em.cluster_thresh,centroids,uc)
    log.info("vsearch command was:\n%s" % (cmd))

    check_call(cmd, shell=True, stdout=sys.stdout, stderr=sys.stderr)

    #now meging cluster abundances and writing out the fasta file with merged abundancance over cutoff
    log.warning("Minimum abundance for estimated %.2dX coverage is %.6f"
                % (em.cov_thresh, em.prob_min))
    clustered_fasta=os.path.join(working_dir,em.output_files_prefix+"_iter."'%02d'%em.max_iterations+"_"'%.6f'%em.prob_min+".fasta")
    outfile = open(clustered_fasta, 'w')
    size_pattern = re.compile(r'NormPrior=([0-9\.]+)')

    cluster_sizes = {}
    for line in open(uc, 'r'):
        atoms = line.strip().split('\t')
        if atoms[0] == 'S':  # seed
            # get seed size, add to current cluster size
            cluster_sizes[atoms[8]] = cluster_sizes.get(atoms[8], 0) + float(size_pattern.search(atoms[8]).groups()[0])
        elif atoms[0] == 'H':  # hit
            # get hit size, add to current cluster size
            cluster_sizes[atoms[9]] = cluster_sizes.get(atoms[9], 0) + float(size_pattern.search(atoms[8]).groups()[0])
        elif atoms[0] == 'C':
            break
        else:
            raise ValueError, "Unknown code in uc file: %s" % atoms[0]


    good_ids = {}
    for line in open(centroids, 'r'):
        if line.startswith('>'):
            ID = line.strip().split(">")[1]
            if cluster_sizes[ID] >= em.prob_min:
                good_ids[ID]=((size_pattern.sub('comb_abund=%f'%cluster_sizes[ID], line)))  # collect IDs of those sequences whose combined cluster abundance is over the calculated$

    # header should be just containing comb_abund.  As it stands, haven't replaced/removed Prior=
    for record in SeqIO.parse(open(centroids,'r'),'fasta'):
        if record.description in good_ids:
            atoms = good_ids[record.description].strip(">").split(" ")
            record.id = atoms[0]
            record.description = " ".join(atoms[1:])
            SeqIO.write(record, outfile, 'fasta')

    outfile.close()
    os.remove(centroids)
    os.remove(uc)


def dependency_check():
    """
    check presense, versions of programs used in emirge
    TODO: right now just checking vsearch, as the command line params
    and behavior are finicky and seem to change from version to
    version
    """
    # vsearch
    working_maj = 1
    working_minor = 1
    working_minor_minor = 0
    match = re.search(r'vsearch([^ ])* v([0-9]*)\.([0-9]*)\.([0-9]*)',
                      Popen("vsearch --version", shell=True, stdout=PIPE).stdout.read())
    if match is None:
        log.critical("FATAL: vsearch not found in path!")
        exit(0)
    binary_name, vsearch_major, vsearch_minor, vsearch_minor_minor = match.groups()
    vsearch_major = int(vsearch_major)
    vsearch_minor = int(vsearch_minor)
    vsearch_minor_minor = int(vsearch_minor_minor)
    if vsearch_major < working_maj or \
       (vsearch_major == working_maj and (vsearch_minor < working_minor or \
                                          (vsearch_minor == working_minor and vsearch_minor_minor < working_minor_minor))):
        log.critical("FATAL: vsearch version found was %s.%s.%s.\n"
                     "emirge works with version >=  %s.%s.%s\n"
                     "vsearch has different command line arguments and minor bugs"
                     "in previous versions that can cause problems."
                     % (vsearch_major, vsearch_minor, vsearch_minor_minor,
                        working_maj, working_minor, working_minor_minor))
        exit(0)


def main(argv=sys.argv[1:]):
    """
    command line interface to emirge

    """
    dependency_check()

    parser = OptionParser(USAGE)

    # REQUIRED
    group_reqd = OptionGroup(parser, "Required flags",
                             "These flags are all required to run EMIRGE, and may be supplied in any order.")

    group_reqd.add_option("-1", dest="fastq_reads_1", metavar="reads_1.fastq[.gz]",
                      type="string",
                      help="path to fastq file with \\1 (forward) reads from paired-end sequencing run, or all reads from single-end sequencing run.  File may optionally be gzipped.  EMIRGE expects ASCII-offset of 64 for quality scores (but see --phred33).  (Note that running EMIRGE with single-end reads is largely untested.  Please let me know how it works for you.)")
    group_reqd.add_option("-f", "--fasta_db",
                      type="string",
                      help="path to fasta file of candidate SSU sequences")
    group_reqd.add_option("-b", "--bowtie_db",
                      type="string",
                      help="precomputed bowtie index of candidate SSU sequences (path to appropriate prefix; see --fasta_db)")
    group_reqd.add_option("-l", "--max_read_length",
                      type="int", default=0,
                      help="""length of longest read in input data.""")
    group_reqd.add_option("-o","--output_files_prefix",
                        type="string", default="EMIRGE",
                        help="prefix to be used for final output fasta file and EMIRGE sequences reconstructed") 
    group_reqd.add_option("-B", "--bowtie2_db",
                      type="string",
                      help="precomputed bowtie2 index of candidate SSU sequences (path to appropriate prefix; see --fasta_db)")
    parser.add_option_group(group_reqd)

    # REQUIRED for paired end
    group_reqd_PE = OptionGroup(parser, "Required flags for paired-end reads",
                             "These flags are required to run EMIRGE when you have paired-end reads (the standard way of running EMIRGE), and may be supplied in any order.")
    group_reqd_PE.add_option("-2", dest="fastq_reads_2", metavar="reads_2.fastq",
                      type="string",
                      help="path to fastq file with \\2 (reverse) reads from paired-end run.  File must be unzipped for mapper.  EMIRGE expects ASCII-offset of 64 for quality scores (but see --phred33).")
    group_reqd_PE.add_option("-i", "--insert_mean",
                      type="int", default=0,
                      help="insert size distribution mean.")
    group_reqd_PE.add_option("-s", "--insert_stddev",
                      type="int", default=0,
                      help="insert size distribution standard deviation.")
    parser.add_option_group(group_reqd_PE)

    # OPTIONAL
    group_opt = OptionGroup(parser, "Optional parameters",
                             "Defaults should normally be fine for these options in order to run EMIRGE")
    group_opt.add_option("-n", "--iterations",
                      type="int", default=40,
                      help="""Number of iterations to perform.  It may be necessary to use more iterations for more complex samples (default=%default)""")
    group_opt.add_option("-a", "--processors",
                      type="int", default=multiprocessing.cpu_count(),
                      help="""Number of processors to use in the mapping steps.  You probably want to raise this if you have the processors. (default: use all available processors)""")
    group_opt.add_option("-m", "--mapping",
                         type="string",
                         help="path to precomputed initial mapping (bam file).  If not provided, an initial mapping will be run for you.")
    group_opt.add_option("-p", "--snp_fraction_thresh",
                      type="float", default="0.04",
                      help="If fraction of variants in a candidate sequence exceeds this threhold, then split the candidate into two sequences for next iteration.  See also --variant_fraction_thresh. (default: %default)")
    group_opt.add_option("-v", "--variant_fraction_thresh",
                      type="float", default="0.1",
                      help="minimum probability of second most probable base at a site required in order to call site a variant.  See also --snp_fraction_thresh.  (default: %default)")
    group_opt.add_option("-j", "--join_threshold",
                      type="float", default="0.97",
                      help="If two candidate sequences share >= this fractional identity over their bases with mapped reads, then merge the two sequences into one for the next iteration.  (default: %default; valid range: [0.95, 1.0] ) ")
    group_opt.add_option("--meta", action="store_true",default="False",
                        help="If input reads are metagenomic, specify --meta to do a pre-mapping step.")
    group_opt.add_option("-d", "--debug",
                        action="store_true", default=False,
                        help="print debug information")
    group_opt.add_option("-q", "--quiet",
                         action="store_true", default=False,
                         help="be less verbose")
    group_opt.add_option("--indel_thresh",
                        type="float",default="0.3",
                        help="temporary flag to test indel thresholds")

    # DEPRECIATED
    # group_opt.add_option("-c", "--min_depth",
    #                   type="float",
    #                   default = 3,
    #                   help = "minimum average read depth below which a candidate sequence is discarded for next iteration(default: %default)")
    group_opt.add_option("-c", "--min_length_coverage",
                      type="float",
                      default = 0.3,
                      help = "minimum fraction of the length of a candidate reference sequence that must be covered by mapped reads.  If not met, a candidate sequence is discarded for the next iteration.  (default: %default; valid range: (0.0, 1.0])")
    group_opt.add_option("--nice_mapping",
                      type="int",
                      help="""If set, during mapping phase, the mapper will be "niced" by the Linux kernel with this value (default: no nice)""")
    # group_opt.add_option("-e", "--save_every",
    #                   type="int", default=10,
    #                   help="""every SAVE_EVERY iterations, save the programs state.  This allows you to run further iterations later starting from these save points.  The program will always save its state after the final iteration.  (default=%default)""")
    group_opt.add_option("--phred33",
                         action="store_true", default=False,
                         help="Illumina quality values in fastq files are the (fastq standard) ascii offset of Phred+33.  This is the new default for Illumina pipeline >= 1.8. DEFAULT is still to assume that quality scores are Phred+64")

    group_opt.add_option('-t', '--min_coverage_threshold',
                           type='int', default='20',
                           help="Expected minimum depth coverage.  Sequences are only included in output FASTA files if their expected coverage is greater than this value (calculated based on EMIRGE-estimated abundance and total number of reads mapped).  Default: %default")

    # --- HIDDEN --- for debugging or special use case

    # this option randomizes the priors calculated for algorithm
    # initialization.  Useful for testing how init affects final
    # sequences and how likely we are to get stuck in a local maxima.
    group_opt.add_option("--randomize_init_priors",
                         action="store_true", default=False,
                         help=SUPPRESS_HELP)

    # if this flag is set, then it is assumed that N reads in input
    # files are labeled with integer names from 0 to N-1, and the read
    # files will not be rewritten as a first step by emirge
    group_opt.add_option("--no_rewrite_reads",
                         action="store_true", default=False,
                         help=SUPPRESS_HELP)
    # --- END HIDDEN ---

    parser.add_option_group(group_opt)
    # # RESUME
    group_resume = OptionGroup(parser, "Resuming iterations",
                              "These options allow you to resume from a previously interrupted run.  EMIRGE will look for the last good iteration and begin with the candidate SSU sequences and priors (current abundance estimates) from that iteration.  Currently, there is only one option associate with resuming iterations: --resume.  The following options cannot be changed from the inital command, and if supplied with --resume, are ignored: -1, -2, --fasta_db, --bowtie_db, --mapping")
    group_resume.add_option("-r", "--resume", action="store_true",
                            help="Resume iterations with the priors and current SSU sequences from the last succesful iteration.")
    # parser.add_option_group(group_resume)

    # ACTUALLY PARSE ARGS
    (options, args) = parser.parse_args(argv)

    # configure log level:
    log.setup(quiet=options.quiet, debug=options.debug)

    # minimal sanity checking of input
    if len(args) !=1:
        parser.error("DIR is required, and all options except DIR should have a flag associated with them (options without flags: %s)"%args)
    if options.join_threshold < 0.95 or options.join_threshold > 1:
        parser.error("join_threshold must be between [0.95, 1.0].  You supplied %.3f. (see --help)"%options.join_threshold)
    if options.min_length_coverage is not None:
        if options.min_length_coverage <= 0 or options.min_length_coverage >= 1:
            parser.error("--min_length_coverage (-c) must be between (0.0, 1.0).  You supplied %.3f. (see --help)"%options.min_length_coverage)

    for filename_option_string in ["fastq_reads_1", "fastq_reads_2", "fasta_db"]:
        filename_option = getattr(options, filename_option_string)
        if filename_option is not None:
            if not os.path.exists(filename_option):
                parser.error("file not found for --%s: %s"%(filename_option_string, filename_option))

    working_dir = os.path.abspath(args[0])

    sys.stdout.write("""If you use EMIRGE in your work, please cite these manuscripts, as appropriate.

Miller CS, Baker BJ, Thomas BC, Singer SW, Banfield JF (2011)
EMIRGE: reconstruction of full-length ribosomal genes from microbial community short read sequencing data.
Genome biology 12: R44. doi:10.1186/gb-2011-12-5-r44.

Miller CS, Handley KM, Wrighton KC, Frischkorn KR, Thomas BC, Banfield JF (2013)
Short-Read Assembly of Full-Length 16S Amplicons Reveals Bacterial Diversity in Subsurface Sediments.
PloS one 8: e56018. doi:10.1371/journal.pone.0056018.\n\n""")

    sys.stdout.write("imported _emirge C functions from: %s\n" % (amplicon.__file__))
    sys.stdout.write("Command:\n")
    sys.stdout.write(' '.join([__file__] + argv))
    sys.stdout.write('\n\n')
    total_start_time = time()
    sys.stdout.write("EMIRGE started at %s\n" % (ctime()))
    sys.stdout.flush()

    # some more sanity checking of options/args
    # RESUME case
    if options.resume:
        if not os.path.exists(working_dir):
            parser.error("You specified --resume, but %s does not exist"
                         % working_dir)
        # find last good directory
        pat = re.compile(r'iter.([0-9]{2,})$')
        current_i = -1
        # basically, this code just finds the second to last directory
        # available and calls that the last successfully completed directory.
        # If the sam file is not zipped, because the failure happened during
        # zipping, it zips it up.
        for lsname in sorted(os.listdir(working_dir)):
            if not os.isdir(lsname): continue
            try:
                this_i = int(pat.search(lsname).groups()[0])
            except AttributeError:  # no match
                continue
            if this_i > current_i:
                if not os.path.exists(os.path.join(working_dir,
                                                   "iter.%02i" % this_i + 1)):
                    continue
                else:
                    pass  # MARK -- need to finish


    # NORMAL case
    else:
        # below here, means that we are handling the NEW case (as opposed to resume)
        required = ["fastq_reads_1", "fasta_db", "bowtie_db", "max_read_length"]
        if options.fastq_reads_2 is not None:
            if options.fastq_reads_2.endswith('.gz'):
                parser.error("Read 2 file cannot be gzipped (see --help)")
            required.extend(["insert_mean", "insert_stddev"])

        for o in required:
            if getattr(options, o) is None or getattr(options, o) == 0:
                if o == 'bowtie_db':
                    if options.fasta_db:
                        parser.error("Bowtie index is missing (--bowtie_db). You need to build it before running EMIRGE\nTry:\n\nbowtie-build %s bowtie_prefix" % options.fasta_db)
                    else:
                        parser.error("Bowtie index is missing (--bowtie_db). You need to build it before running EMIRGE\nTry:\n\nbowtie-build candidate_db.fasta bowtie_prefix")
                elif o == 'fasta_db':
                    parser.error("Fasta file for candidate database is missing. Specify --fasta_db. (try --help for more information)")
                else:
                    parser.error("--%s is required, but is not specified (try --help)"%(o))
	
	if not os.path.exists(working_dir):
            os.mkdir(working_dir)
        else:
            if len(os.listdir(working_dir)) > 1:  # allow 1 file in case log file is redirected here.
                print >> sys.stderr, os.listdir(working_dir)
                parser.error("Directory not empty: %s\n"
                             "It is recommended you run emirge in a new directory "
                             "each run; delete this directory or specifiy a new one."
                             % working_dir)

    # clean up options to be absolute paths
    for o in ["fastq_reads_1", "fastq_reads_2", "fasta_db", "bowtie_db", "mapping"]:
        current_o_value = getattr(options, o)
        if current_o_value is not None:
            setattr(options, o, os.path.abspath(current_o_value))


    # IF METAGENOMIC, DO PRE-MAPPING
    if options.meta:
        # do the premapping and then feed those saved reads in as reads1_filepath and reads2_filepath
        pre_mapping_dir = os.path.join(working_dir, "pre_mapping")
        do_premapping(pre_mapping_dir,options)
        options.fastq_reads_1=os.path.join(pre_mapping_dir, "pre_mapped_reads.1.fastq")
        options.fastq_reads_2=os.path.join(pre_mapping_dir, "pre_mapped_reads.2.fastq")


    # finally, CREATE EM OBJECT
    em = EM(reads1_filepath=options.fastq_reads_1,
            reads2_filepath=options.fastq_reads_2,
            insert_mean=options.insert_mean,
            insert_sd=options.insert_stddev,
            output_files_prefix=options.output_files_prefix,
            cov_thresh=options.min_coverage_threshold,
            max_read_length=options.max_read_length,
            cluster_thresh=options.join_threshold,
            indel_thresh=options.indel_thresh,
            n_cpus=options.processors,
            cwd=working_dir,
            reads_ascii_offset={False: 64, True: 33}[options.phred33],
            rewrite_reads=not options.no_rewrite_reads)

    options.fastq_reads_1 = em.reads1_filepath # change these if necessary for do_initial_mapping.
    options.fastq_reads_2 = em.reads2_filepath

    # DO INITIAL MAPPING if not provided with --mapping
    if options.mapping is None:
        if options.bowtie2_db is not None:
            options.mapping = do_initial_mapping_bt2(em, working_dir, options)
        else:
            options.mapping = do_initial_mapping(em, working_dir, options)


    # if >= this percentage of bases are minor alleles, split candidate sequence
    em.snp_percentage_thresh = options.snp_fraction_thresh
    # if prob(N) for minor allele base N is >= this threshold, call site a minor allele
    em.snp_minor_prob_thresh = options.variant_fraction_thresh
    if options.min_length_coverage is not None:
        em.min_length_coverage = options.min_length_coverage
    # em.min_depth = options.min_depth  # DEPRECIATED
    if options.nice_mapping is not None:
        em.mapping_nice = options.nice_mapping

    if options.randomize_init_priors:
        print >> sys.stderr, "*" * 60
        print >> sys.stderr, "DEBUG: initialized priors will be randomized for testing purposes"
    em.initialize_EM(options.mapping, options.fasta_db, randomize_priors=options.randomize_init_priors)

    # BEGIN ITERATIONS
    do_iterations(em, max_iter=options.iterations, save_every=None)

    # WRITE THE OUTPUT FASTA FILES- renames and writes raw file and file with only seqs having estimated coverage over specified threshold (default=20X)
    post_process(em, working_dir)

    # print some info to user about final files produced, brief description, filename
    # TODO
    
    sys.stdout.write("EMIRGE finished at %s.  Total time: %s\n"%(ctime(), timedelta(seconds = time()-total_start_time)))


if __name__ == '__main__':
    main()
