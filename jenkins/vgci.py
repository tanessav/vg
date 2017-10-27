#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import logging
import shutil
import subprocess
import tempfile
import textwrap
import filecmp
import pytest
from unittest import TestCase, skip, TestLoader, TextTestRunner
from urlparse import urlparse
from uuid import uuid4
import os, sys
import argparse
import collections
import timeout_decorator
import urllib2
import shutil
import glob
import traceback
import io
from datetime import datetime

import tsv

from toil_vg.vg_mapeval import get_default_mapeval_options, make_mapeval_plan, run_mapeval
from toil_vg.vg_toil import parse_args
from toil_vg.context import Context
from toil_vg.vg_common import make_url, toil_call

from toil.common import Toil
from toil.job import Job

log = logging.getLogger(__name__)


class VGCITest(TestCase):
    """
    Continuous Integration VG tests.  All depend on toil-vg being installed.  Along with 
    toil[aws,mesos].  They are somewhat derived from the toil-vg unittests, but are
    much slower.  
    """
    def setUp(self):
        # Make sure logging is available for all the tests
        logging.basicConfig()

        self.workdir = tempfile.mkdtemp()
        self.tempdir = tempfile.mkdtemp()

        # for checking calling f1
        self.f1_threshold = 0.005
        # What (additional) portion of reads are allowed to get worse scores
        # when moving to a more inclusive reference?
        self.worse_threshold = 0.005
        self.input_store = 'https://cgl-pipeline-inputs.s3.amazonaws.com/vg_cgl/bakeoff'
        self.vg_docker = None
        self.container = None # Use default in toil-vg, which is Docker
        self.verify = True
        self.do_teardown = True
        self.baseline = 's3://cgl-pipeline-inputs/vg_cgl/vg_ci/jenkins_regression_baseline'
        self.cores = 8

        self.loadCFG()

        # These are samples that are in 1KG but not in the bakeoff snp1kg graphs. 
        self.bakeoff_removed_samples = set(['NA128{}'.format(x) for x in range(77, 94)])
                
    def tearDown(self):
        shutil.rmtree(self.tempdir)        
        if self.do_teardown:
            shutil.rmtree(self.workdir)

    def loadCFG(self):
        """ It's a hassle passing parameters through pytest.  Hack
        around for now by loading from a file of key/value pairs. """
        if os.path.isfile('vgci_cfg.tsv'):
            with io.open('vgci_cfg.tsv', 'r', encoding='utf8') as f:
                for line in f:
                    toks = line.split()
                    if len(toks) == 2 and toks[0][0] != '#':
                        # override vg docker (which defaults to value from vg_config.py)
                        if toks[0] == 'vg-docker-version':
                            self.vg_docker = toks[1]
                        # can use "Docker", "Singularity" or "None" (the string) as a container system
                        if toks[0] == 'container':
                            self.container = toks[1]
                        # dont verify output.  tests will pass if they dont crash or timeout
                        elif toks[0] == 'verify' and toks[1].lower() == 'false':
                            self.verify = False
                        # dont delete the working directory
                        elif toks[0] == 'teardown' and toks[1].lower() == 'false':
                            self.do_teardown = False
                        # override the working directory (defaults to temp)
                        elif toks[0] == 'workdir':
                            self.workdir = toks[1]
                        elif toks[0] == 'baseline':
                            self.baseline = toks[1]
                        elif toks[0] == 'cores':
                            self.cores = int(toks[1])

    def _jobstore(self, tag = ''):
        return os.path.join(self.workdir, 'jobstore{}'.format(tag))

    def _outstore_name(self, tag = ''):
        return 'outstore-{}'.format(tag)
    
    def _outstore(self, tag = ''):
        return os.path.join(self.workdir, self._outstore_name(tag))

    def _input(self, filename):
        return os.path.join(self.input_store, filename)

    def _bakeoff_coords(self, region):
        if region == 'BRCA1':
            return 17, 43044293
        elif region == 'BRCA2':
            return 13, 32314860
        elif region == 'SMA':
            return 5, 69216818
        elif region == 'LRC-KIR':
            return 19, 54025633
        elif region == 'MHC':
            return 6, 28510119
        elif 'CHR' in region:
            return int(region.replace('CHR', '')), 0
        return None, None
        
    def _read_baseline_file(self, tag, path):
        """ read a (small) text file from the baseline store """
        if self.baseline.startswith('s3://'):
            toks = self.baseline[5:].split('/')
            bname = toks[0]
            keyname = '/{}/outstore-{}/{}'.format('/'.join(toks[1:]), tag, path)
            
            # Convert to a public HTTPS URL
            url = 'https://{}.s3.amazonaws.com{}'.format(bname, keyname)
            # And download it
            
            try:
                connection = urllib2.urlopen(url)
                return unicode(connection.read())
            except urllib2.HTTPError as e:
                if e.code == 404:
                    # Baseline file doesn't yet exist. Give an empty string.
                    return ""
                else:
                    # Something else is wrong
                    raise
        else:
            # Assume it's a raw path.
            with io.open(os.path.join(self.baseline, 'outstore-{}'.format(tag), path), 'r', encoding='utf8') as f:
                return f.read()

    def _get_remote_file(self, src, tgt):
        """
        get a file from a store
        
        src must be a URL.
        
        """
        if not os.path.exists(os.path.dirname(tgt)):
            os.makedirs(os.path.dirname(tgt))
            
        if src.startswith('s3://'):
            toks = src[5:].split('/')
            bname = toks[0]
            keyname = '/' + '/'.join(toks[1:])

            # Convert to a public HTTPS URL
            src = 'https://{}.s3.amazonaws.com{}'.format(bname, keyname)
        
        log.info('Download {}...\n'.format(src))
        
        with open(tgt, 'w') as f:
            # Download the file from the URL
            # DON'T use an encoding here; the file may be binary
            connection = urllib2.urlopen(src)
            shutil.copyfileobj(connection, f)

    def _begin_message(self, name = None, is_tsv = False, ):
        """ Used by mine-logs.py to flag that we're about to write something we want to mine
        Anything in stdout that's not within these tags does not make it to the report """
        token = '<VGCI'
        if name:
            token += ' name = "{}"'.format(name)
        if is_tsv:
            token += ' tsv = "True"'
        token += '>'
        print '\n{}'.format(token)
    
    def _end_message(self):
        """ Finish writing something mineable to stdout """
        print '</VGCI>\n'
                
    def _toil_vg_index(self, chrom, graph_path, xg_path, gcsa_path, misc_opts, dir_tag, file_tag):
        """ Wrap toil-vg index.  Files passed are copied from store instead of computed """
        job_store = self._jobstore(dir_tag)
        out_store = self._outstore(dir_tag)
        opts = '--realTimeLogging --logInfo '
        if self.vg_docker:
            opts += '--vg_docker {} '.format(self.vg_docker)
        if self.container:
            opts += '--container {} '.format(self.container)
        if chrom:
            opts += '--chroms {} '.format(chrom)
        if graph_path:
            opts += '--graphs {} '.format(graph_path)
        if xg_path:
            opts += '--skip_xg '
            self._get_remote_file(xg_path, os.path.join(out_store, os.path.basename(xg_path)))
        if gcsa_path and (not misc_opts or '--skip_gcsa' not in misc_opts):
            opts += '--skip_gcsa '
            self._get_remote_file(gcsa_path, os.path.join(out_store, os.path.basename(gcsa_path)))
            self._get_remote_file(gcsa_path + '.lcp', os.path.join(out_store, os.path.basename(gcsa_path) + '.lcp'))
        opts += '--index_name {}'.format(file_tag)
        if misc_opts:
            opts += ' {} '.format(misc_opts)
        
        cmd = 'toil-vg index {} {} {}'.format(job_store, out_store, opts)
        
        subprocess.check_call(cmd, shell=True)        
        
        
    def _toil_vg_run(self, sample_name, chrom, graph_path, xg_path, gcsa_path, fq_path,
                     true_vcf_path, fasta_path, interleaved, multipath, misc_opts, tag):
        """ Wrap toil-vg run as a shell command.  Expects reads to be in single fastq
        inputs can be None if toil-vg supports not having them (ie don't need to 
        include gcsa_path if want to reindex)
        """

        job_store = self._jobstore(tag)
        out_store = self._outstore(tag)
        opts = '--realTimeLogging --logInfo '
        if self.vg_docker:
            opts += '--vg_docker {} '.format(self.vg_docker)
        if self.container:
            opts += '--container {} '.format(self.container)
        if chrom:
            opts += '--chroms {} '.format(chrom)
        if graph_path:
            opts += '--graphs {} '.format(graph_path)
        if xg_path:
            opts += '--xg_index {} '.format(xg_path)
        if gcsa_path:
            opts += '--gcsa_index {} '.format(gcsa_path)
        if fq_path:
            opts += '--fastq {} '.format(fq_path)
        if true_vcf_path:
            opts += '--vcfeval_baseline {} '.format(true_vcf_path)
            opts += '--vcfeval_fasta {} '.format(fasta_path)
            opts += '--vcfeval_opts \" --ref-overlap\" '
        if interleaved:
            opts += '--interleaved '
        if multipath:
            opts += '--multipath '
        if misc_opts:
            opts += ' {} '.format(misc_opts)
        # don't waste time sharding reads since we only run on one node
        opts += '--single_reads_chunk '
        opts += '--gcsa_index_cores {} --kmers_cores {} \
        --alignment_cores {} --calling_cores {} --call_chunk_cores {} --vcfeval_cores {} '.format(
            self.cores, self.cores, self.cores, max(1, self.cores / 4),
            max(1, self.cores / 2), self.cores)
        
        cmd = 'toil-vg run {} {} {} {}'.format(job_store, sample_name, out_store, opts)
        
        subprocess.check_call(cmd, shell=True)

    def _make_thread_indexes(self, sample, vg_file, vcf_file, region, tag=''):
        """ Given a graph, then we extract two threads from the
        given sample as their own graphs, then return an xg index for each.
        this only supports one chromosome at a time, presently.
        the indexes are written as thread_0.xg and thread_1.xg in the
        output store (derived from tag parameter like other methods)
        """
        job_store = self._jobstore(tag)
        out_store = self._outstore(tag)
        out_store_name = self._outstore_name(tag)

        # What do we want to override from the default toil-vg config?
        overrides = argparse.Namespace(
            # toil-vg options
            vg_docker = self.vg_docker,
            container = self.container,
            # Toil options
            realTimeLogging = True,
            logLevel = "INFO",
            maxCores = self.cores
        )

        # Make the context
        context = Context(out_store, overrides)

        # The unfiltered and filtered vcf file
        uf_vcf_file = os.path.join(self.workdir, 'uf-' + os.path.basename(vcf_file))
        f_vcf_file = os.path.join(self.workdir, 'f-' + os.path.basename(vcf_file))
        if not f_vcf_file.endswith('.gz'):
            f_vcf_file += '.gz'
        
        # Get the inputs
        self._get_remote_file(vg_file, os.path.join(out_store, os.path.basename(vg_file)))
        self._get_remote_file(vcf_file, uf_vcf_file)

        # Reduce our VCF to just the sample of interest to save time downstream
        with context.get_toil(job_store) as toil:
            cmd = ['bcftools', 'view', os.path.basename(uf_vcf_file), '-s', sample, '-O', 'z']
            toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                     work_dir = os.path.abspath(self.workdir),
                                     out_path = os.path.abspath(f_vcf_file)))
            cmd = ['tabix', '-f', '-p', 'vcf', os.path.basename(f_vcf_file)]
            toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                     work_dir = os.path.abspath(self.workdir)))
        os.remove(uf_vcf_file)
        
        # Make the xg with gpbwt of the input graph
        index_name = 'index-gpbwt'
        chrom, offset = self._bakeoff_coords(region)
        self._toil_vg_index(chrom, vg_file, None, None,
                            '--vcf_phasing {} --skip_gcsa --xg_index_cores {}'.format(
                                os.path.abspath(f_vcf_file), self.cores), tag, index_name)
        index_path = os.path.join(out_store, index_name + '.xg')
        os.remove(f_vcf_file)
        os.remove(f_vcf_file + '.tbi')
        
        # Extract both haplotypes of the given sample as their own graphs
        # (this is done through vg directly)
        for hap in [0, 1]:
            tmp_thread_path = os.path.abspath(os.path.join(self.workdir, 'thread_{}.vg'.format(hap)))

            # This is straght from Erik.  We mush together the original graph
            # (without paths) and the thread path from the xg index
            with context.get_toil(job_store) as toil:
                cmd = ['vg', 'mod', '-D', os.path.join(out_store_name, os.path.basename(vg_file))]
                toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                         work_dir = os.path.abspath(self.workdir),
                                         out_path = tmp_thread_path))

                # note: I'm not sure why that _0 is there but all threads seem
                # to have names like _thread_NA12878_17_0_0 and _thread_NA12878_17_1_0
                cmd = ['vg', 'find', '-q', '_thread_{}_{}_{}_0'.format(sample, chrom, hap),
                       '-x', os.path.join(out_store_name, os.path.basename(index_path))]
                toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                         work_dir = os.path.abspath(self.workdir),
                                         out_path = tmp_thread_path,
                                         out_append = True))

                # Then we trim out anything other than our thread path
                cmd = ['vg', 'mod', '-N', os.path.basename(tmp_thread_path)]
                toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                         work_dir = os.path.abspath(self.workdir),
                                         out_path = tmp_thread_path + '.drop'))

            # Index the thread graphs so we can simulate from them
            self._toil_vg_index(chrom, tmp_thread_path + '.drop', None, None,
                                '--skip_gcsa', tag, 'thread_{}'.format(hap))

            # They're in a tmp work dir so this is probably overkill
            os.remove(tmp_thread_path)
            os.remove(tmp_thread_path + '.drop')

        return index_path, os.path.join(out_store, 'thread_0.xg'), os.path.join(out_store, 'thread_1.xg')

    def _verify_f1(self, sample, tag='', threshold=None):
        # grab the f1.txt file from the output store
        if sample:
            f1_name = '{}_vcfeval_output_f1.txt'.format(sample)
        else:
            f1_name = 'vcfeval_output_f1.txt'
        f1_path = os.path.join(self._outstore(tag), f1_name)
        with io.open(f1_path, 'r', encoding='utf8') as f1_file:
            f1_score = float(f1_file.readline().strip())
        baseline_f1 = float(self._read_baseline_file(tag, f1_name).strip())
        
        # compare with threshold
        if not threshold:
            threshold = self.f1_threshold

        # print the whole table in tags that mine-logs can read
        self._begin_message('vcfeval Results'.format(
            f1_score, baseline_f1, threshold), is_tsv=True)
        summary_path = f1_path[0:-6] + 'summary.txt'
        with io.open(summary_path, 'r', encoding='utf8') as summary_file:
            for i, line in enumerate(summary_file):
                if i != 1:
                    toks = line.split()
                    if i == 0:
                        toks = toks[0:-1] + ['F1', 'Baseline F1', 'Test Threshold']
                    elif i == 2:
                        toks += [baseline_f1, threshold]
                    elif i > 2:
                        toks += ['N/A', 'N/A']
                    print '\t'.join([unicode(tok) for tok in toks])
        self._end_message()

        self.assertGreaterEqual(f1_score, baseline_f1 - threshold)

    def _test_bakeoff(self, region, graph, skip_indexing, multipath=False, tag_ext='', misc_opts=None):
        """ Run bakeoff F1 test for NA12878 """
        assert not tag_ext or tag_ext.startswith('-')
        tag = '{}-{}{}'.format(region, graph, tag_ext)
        chrom, offset = self._bakeoff_coords(region)        
        if skip_indexing:
            xg_path = None
            gcsa_path = self._input('{}-{}.gcsa'.format(graph, region))
        else:
            xg_path = None
            gcsa_path = None
        extra_opts = '--vcf_offsets {}'.format(offset)
        if misc_opts:
            extra_opts += ' {}'.format(misc_opts)
        
        self._toil_vg_run('NA12878', chrom,
                          self._input('{}-{}.vg'.format(graph, region)),
                          xg_path, gcsa_path,
                          self._input('platinum_NA12878_{}.fq.gz'.format(region)),
                          self._input('platinum_NA12878_{}.vcf.gz'.format(region)),
                          self._input('chr{}.fa.gz'.format(chrom)), True, multipath,
                          extra_opts, tag)

        if self.verify:
            self._verify_f1('NA12878', tag)

    def _mapeval_vg_run(self, reads, base_xg_path, sim_xg_paths,
                        source_path_names, fasta_path, test_index_bases,
                        test_names, score_baseline_name,  multipath,
                        paired_only, sim_opts, sim_fastq, tag):
        """ Wrap toil-vg mapeval. 
        
        Evaluates realignments (to the linear reference and to a set of graphs)
        of reads simulated from a single "base" graph. Realignments are
        evaluated based on how close the realignments are to the original
        simulated source position. Simulations are done inside this function.

        sim_xg_paths are xg filenames used for simulation. base_xg_path is an xg
        filename used for everything else like annotation and mapping.
        sim_xg_paths can be [base_xg_path]
        
        Simulates the given number of reads (reads), from the given XG files
        (sim_xg_paths), optionally restricted to a set of named embedded paths
        (source_path_namess). Uses the given FASTA (fasta_path) as a BWA
        reference for comparing vg and BWA alignments within mapeval.
        (Basically, BWA against the linear reference functions as a negative
        control "graph" to compare against the real test graphs.)
        
        test_index_bases specifies a list of basenames (without extension) for a
        .xg, .gcsa, and .gcsa.lcp file set, one per of graph that is to be
        compared.
        
        test_names has one entry per graph to be compared, and specifies where
        the realigned read GAM files should be saved.
        
        score_baseline_name, if not None, is a name from test_names to be used
        as a score baseline for comparing all the realignment scores against.
        
        tag is a unique slug for this test/run, which determines the Toil job
        store name to use, and the location where the output files should be
        saved.
        
        """

        job_store = self._jobstore(tag)
        out_store = self._outstore(tag)

        # start by simulating some reads
        # TODO: why are we using strings here when we could use much safer lists???
        opts = '--realTimeLogging --logInfo '
        if self.vg_docker:
            opts += '--vg_docker {} '.format(self.vg_docker)
        if self.container:
            opts += '--container {} '.format(self.container)
        # note, using the same seed only means something if using same
        # number of chunks.  we make that explicit here
        opts += '--maxCores {} --sim_chunks {} --seed {} '.format(self.cores, 8, 8)
        if sim_opts:
            opts += '--sim_opts \'{}\' '.format(sim_opts)
        if sim_fastq:
            opts += '--fastq {} '.format(sim_fastq)
        opts += '--annotate_xg {} '.format(base_xg_path)
        for source_path_name in source_path_names:
            opts += '--path {} '.format(source_path_name)
        cmd = 'toil-vg sim {} {} {} {} --gam {}'.format(
            job_store, ' '.join(sim_xg_paths), reads / 2, out_store, opts)
        subprocess.check_call(cmd, shell=True)

        # then run mapeval
        
        # What do we want to override from the default toil-vg config?
        overrides = argparse.Namespace(
            # toil-vg options
            vg_docker = self.vg_docker,
            container = self.container,
            alignment_cores = self.cores,
            # Toil options
            realTimeLogging = True,
            logLevel = "INFO",
            maxCores = self.cores,
            # toil-vg map options
            # don't waste time sharding reads since we only run on one node
            single_reads_chunk = True
        )
        
        # Make the context
        context = Context(out_store, overrides)
        
        # And what options to configure the mapeval run do we want? These have
        # to get turned into a plan in order to import all the files with names
        # derived algorithmically from the names given here. TODO: move
        # positional/required arguments out of this somehow? So we can just use
        # this to get the default optional settings and fill in the required
        # things as file IDs?
        mapeval_options = get_default_mapeval_options(os.path.join(out_store, 'true.pos'))
        mapeval_options.bwa = True
        mapeval_options.paired_only = paired_only        
        mapeval_options.fasta = make_url(fasta_path)
        mapeval_options.index_bases = [make_url(x) for x in test_index_bases]
        mapeval_options.gam_names = test_names
        mapeval_options.gam_input_reads = make_url(os.path.join(out_store, 'sim.gam'))
        # We have 150 bp reads reduced to a point position, at a resolution of
        # only the nearest 100 bp (on the primary graph). How close do two such
        # point positions need to be to say the read is in the right place?
        mapeval_options.mapeval_threshold = 200
        if score_baseline_name is not None:
            mapeval_options.compare_gam_scores = score_baseline_name
        mapeval_options.multipath = multipath
        mapeval_options.ignore_quals = multipath and not sim_fastq            
        
        # Make Toil
        with context.get_toil(job_store) as toil:
            
            # Make a plan by importing those files specified in the mapeval
            # options
            plan = make_mapeval_plan(toil, mapeval_options)
            
            # Make a job to run the mapeval workflow, using all these various imported files.
            main_job = Job.wrapJobFn(run_mapeval,
                                     context, 
                                     mapeval_options, 
                                     plan.xg_file_ids,
                                     plan.gcsa_file_ids, 
                                     plan.id_range_file_ids,
                                     plan.vg_file_ids, 
                                     plan.gam_file_ids, 
                                     plan.reads_gam_file_id, 
                                     plan.fasta_file_id, 
                                     plan.bwa_index_ids, 
                                     plan.bam_file_ids,
                                     plan.pe_bam_file_ids, 
                                     plan.true_read_stats_file_id)
                
            # Output files all live in the out_store, but if we wanted to we could export them also/instead.
            
            # Run the root job
            returned = toil.start(main_job)
            
            # TODO: I want to do the evaluation here, working with file IDs, but
            # since we put the results in the out store maybe it really does
            # make sense to just go through the files in the out store.

    def _filter_position_file(self, position_file, out_file):
        """ Filter reads that fail score check out of a position comparison file
        Return number of reads filtered """

        # check if a read has been filtered by looking in the primary score comparison output
        reads_map = dict()
        def is_filtered(read, method):
            if method not in reads_map:
                reads_map[method] = set()
                try:
                    # -se not currently in filenames. ugh. 
                    name = method[0:-3] if method.endswith('-se') else method
                    score_path = os.path.join(os.path.dirname(position_file),
                                              '{}.compare.primary.scores'.format(name))
                    with io.open(score_path, 'r', encoding='utf8') as score_file:
                        for line in score_file:
                            toks = line.split(", ")
                            if int(toks[1]) < 0:
                                reads_map[method].add(toks[0])
                except:
                    pass
            return read in reads_map[method]

        # scan postions, filtering all reads in our set
        filter_count = 0
        with io.open(position_file, 'r', encoding='utf8') as pf, io.open(out_file, 'w', encoding='utf8') as of:
            for i, line in enumerate(pf):
                toks = line.rstrip().split()
                if i == 0:
                    ridx = toks.index('read')
                    aidx = toks.index('aligner')                
                if i == 0 or not is_filtered(toks[ridx], toks[aidx].strip('"')):
                    of.write(line)
                else:
                    filter_count += 1
        return filter_count
            
    def _mapeval_r_plots(self, tag, positive_control=None, negative_control=None,
                         control_include=['snp1kg', 'primary', 'common1kg'], min_reads_for_filter_plots=100):
        """ Compute the mapeval r plots (ROC and QQ) """
        out_store = self._outstore(tag)
        out_store_name = self._outstore_name(tag)
        job_store = self._jobstore(tag)        

        # What do we want to override from the default toil-vg config?
        overrides = argparse.Namespace(
            container = self.container,
            # Toil options
            realTimeLogging = True,
            logLevel = "INFO",
            maxCores = self.cores
        )

        # Lookup names list with -pe and -se attached
        def pe_se(names):
            names_e = [[x, '{}-se'.format(x), '{}-pe'.format(x)] for x in names if x]
            return [y for x in names_e for y in x]
        
        # Make the context
        context = Context(out_store, overrides)
        with context.get_toil(job_store) as toil:
            try:
                for rscript in ['pr', 'qq', 'roc']:
                    # pull the scripts from where we expect them relative to being in vg/
                    # and put them in the work directory.  This is ugly but keeps the
                    # docker interfacing simple.
                    shutil.copy2('scripts/plot-{}.R'.format(rscript), os.path.abspath(self.workdir))

                    # if controls specified, filter into their own plot so things don't get too busy
                    if positive_control or negative_control:
                        nc_name = 'position.results.no.control.tsv'
                        co_name = 'position.results.control.tsv'
                        with io.open(os.path.join(out_store, 'position.results.tsv'), 'r', encoding='utf8') as pr_file,\
                             io.open(os.path.join(out_store, nc_name), 'w', encoding='utf8') as nc_file,\
                             io.open(os.path.join(out_store, co_name), 'w', encoding='utf8') as co_file:
                            aidx = None
                            for i, line in enumerate(pr_file):
                                toks = line.rstrip().split()
                                if i == 0:
                                    aidx = toks.index('aligner')
                                if i == 0 or toks[aidx].strip('"') in pe_se(control_include + 
                                        [positive_control, negative_control]):
                                    co_file.write(line)
                                if i == 0 or toks[aidx].strip('"') not in  pe_se(
                                        [positive_control, negative_control]):
                                    nc_file.write(line)
                    else:
                        nc_name = 'position.results.tsv'
                        co_name = None

                    plot_names = [(nc_name, '')]
                    if co_name:
                        plot_names.append((co_name, '.control'))

                    # make a plot where we ignore reads that fail score
                    cur_plot_names = [pn for pn in plot_names]
                    for name,tag in plot_names:
                        pf_name = name.replace('.tsv', '.primary.filter.tsv')
                        if self._filter_position_file(
                                os.path.join(out_store, name),
                                os.path.join(out_store, pf_name)) > min_reads_for_filter_plots:
                            plot_names.append((pf_name, tag + '.primary.filter'))
                        else:
                            if os.path.isfile(os.path.join(out_store, pf_name)):
                                os.remove(os.path.join(out_store, pf_name))
                        
                    for tsv_file, out_name in plot_names:
                        cmd = ['Rscript', 'plot-{}.R'.format(rscript),
                               os.path.join(out_store_name, tsv_file),
                               os.path.join(out_store_name, '{}{}.svg'.format(rscript, out_name))]
                        toil.start(Job.wrapJobFn(toil_call, context, cmd,
                                                 work_dir = os.path.abspath(self.workdir)))

                    os.remove(os.path.join(self.workdir, 'plot-{}.R'.format(rscript)))
                    
                if os.path.isfile(os.path.join(self.workdir, 'Rplots.pdf')):
                    os.remove(os.path.join(self.workdir, 'Rplots.pdf'))

            except Exception as e:
                log.warning("Failed to generate ROC and QQ plots with Exception: {}".format(e))
        
                        
    def _tsv_to_dict(self, stats, row_1 = 1):
        """ convert tsv string into dictionary """
        stats_dict = dict()
        for line in stats.split('\n')[row_1:]:
            toks = line.split()
            if len(toks) > 1:
                stats_dict[toks[0]] = [float(x) for x in toks[1:]]
        return stats_dict

    def _verify_mapeval(self, reads, read_source_graph, score_baseline_name,
                        positive_control, negative_control, tag, acc_threshold,
                        auc_threshold):
        """
        Check the simulated mapping evaluation results.
        
        read_source_graph is the name of the graph that the reads were generated
        from; we'll compare the scores realigned to that graph against the
        scores that the generated reads had.
        
        score_baseline_name is the name of the graph we compared scores against;
        we will chack that reads increase in score in the other graphs against
        that graph and complain if they don't. It may be None, in which case
        scores are only compared against the scores the reads got when
        simulated.
        
        """

        # Make some plots in the outstore
        self._mapeval_r_plots(tag, positive_control, negative_control)

        stats_path = os.path.join(self._outstore(tag), 'stats.tsv')
        with io.open(stats_path, 'r', encoding='utf8') as stats:
            stats_tsv = stats.read()
        baseline_tsv = self._read_baseline_file(tag, 'stats.tsv')

        # Dict from aligner to a list of float stat values, in order
        stats_dict = self._tsv_to_dict(stats_tsv)
        # Dict from aligner to a list of float stat values, in order
        baseline_dict = self._tsv_to_dict(baseline_tsv)

        # print out a table of mapeval results
        table_name = 'map eval results'
        if positive_control:
            table_name += ' (*: positive control)'
        if negative_control:
            table_name += ' (**: negative control)'
        self._begin_message(table_name, is_tsv=True)
        
        # How many different columns do we want to see in the stats files?
        # We need to pad shorter rows with 0s
        stats_columns = 5 # read count, accuracy, AUC, QQ-plot r value, max F1
        
        print '\t'.join(['Method', 'Acc.', 'Baseline Acc.', 'AUC', 'Baseline AUC', 'Max F1', 'Baseline F1'])
        for key in sorted(set(baseline_dict.keys() + stats_dict.keys())):
            # What values do we have for the graph this run?
            sval = list(stats_dict.get(key, []))
            while len(sval) < stats_columns:
                sval.append('DNE')
            # And what baseline values do we have stored?
            bval = list(baseline_dict.get(key, []))
            while len(bval) < stats_columns:
                bval.append('DNE')
            
            method = key            
            if not key.endswith('-pe'):
                # to be consistent with plots
                method += '-se'
            if positive_control and key in [positive_control, positive_control + '-pe']:
                method += '*'
            if negative_control and key in [negative_control, negative_control + '-pe']:
                method += '**'
            def r4(s):
                return round(s, 5) if isinstance(s, float) else s 
                
            row = [method]
            for metric_index in [1, 2, 4]:
                # For each metric, compare stat to baseline
                stat_val = unicode(r4(sval[metric_index]))
                baseline_val = unicode(r4(bval[metric_index]))
                if stat_val != 'DNE' and baseline_val != 'DNE':
                    if sval[metric_index] < bval[metric_index]:
                        # Stat got worse
                        stat_val = '↓ {}'.format(stat_val)
                    elif sval[metric_index] > bval[metric_index]:
                        # Stat got better
                        stat_val = '↑ {}'.format(stat_val)
                row.append(stat_val)
                row.append(baseline_val)
                               
            print '\t'.join(row)
        self._end_message()

        # test the mapeval results, only looking at baseline keys
        for key, val in baseline_dict.iteritems():
            if key in stats_dict:
                # For each graph we have a baseline and stats for, compare the
                # columns we actually have in both.
                if len(stats_dict[key]) > 0:
                    self.assertEqual(stats_dict[key][0], reads)
                if len(stats_dict[key]) > 1 and len(val) > 1:
                    # Compare accuracy stats
                    self.assertGreaterEqual(stats_dict[key][1], val[1] - acc_threshold)
                if len(stats_dict[key]) > 2 and len(val) > 2:
                    # Compare AUC stats. Make sure to patch up 0 AUCs from perfect classification.
                    new_auc = stats_dict[key][2] if stats_dict[key][2] != 0 else 1
                    old_auc = val[2] if val[2] != 0 else 1
                    self.assertGreaterEqual(new_auc, old_auc - auc_threshold)
                if len(stats_dict[key]) > 4 and len(val) > 4:
                    self.assertGreaterEqual(stats_dict[key][4], val[4] - acc_threshold)
                if len(stats_dict[key]) != len(val):
                    log.warning('Key {} has {} baseline entries and {} stats'.format(key, len(val), len(stats_dict[key])))
            else:
                log.warning('Key {} from baseline not found in stats'.format(key))
            
        # This holds the condition names we want a better score than
        score_baselines = ['input']
        if score_baseline_name is not None:
            score_baselines.append(score_baseline_name)
            
        for compare_against in score_baselines:
            # For each graph/condition we want to compare scores against
        
            # Now look at the stats for comparing scores on all graphs vs. scores on this particular graph.
            score_stats_path = os.path.join(self._outstore(tag), 'score.stats.{}.tsv'.format(compare_against))
            if os.path.exists(score_stats_path):
                # If the score comparison was run, make sure not too many reads
                # get worse moving from simulated to realigned scores, or moving
                # from the baseline graph to the other (more inclusive) graphs.
                
                try:
                    # Parse out the baseline stat values (not for the baseline
                    # graph; we shouldn't have called these both "baseline")
                    baseline_tsv = self._read_baseline_file(tag, 'score.stats.{}.tsv'.format(compare_against))
                    baseline_dict = self._tsv_to_dict(baseline_tsv)
                except:
                    # Maybe there's no baseline file saved yet
                    # Synthesize one of the right shape
                    baseline_dict = collections.defaultdict(lambda: [0, 0])
                    
                # Parse out the real stat values
                score_stats_dict = self._tsv_to_dict(io.open(score_stats_path, 'r', encoding='utf8').read())
                    
                for key in score_stats_dict.iterkeys():
                    # For every kind of graph
                    
                    if compare_against == 'input' and (key != read_source_graph and
                        key != read_source_graph + '-pe'):
                        # Only compare simulated read scores to the scores the
                        # reads get when aligned against the graph they were
                        # simulated from.
                        continue
                    
                    # Guess where the file for individual read score differences for this graph is
                    # TODO: get this file's name/ID from the actual Toil code
                    read_comparison_path = os.path.join(self._outstore(tag), '{}.compare.{}.scores'.format(key, compare_against))
                    for line in io.open(read_comparison_path, 'r', encoding='utf8'):
                        if line.strip() == '':
                            continue
                        # Break each line of the CSV
                        parts = line.split(', ')
                        # Fields are read name, score difference, aligned score, baseline score
                        read_name = parts[0]
                        score_diff = int(parts[1])
                        
                        if score_diff < 0:
                            # Complain about anyone who goes below 0.
                            log.warning('Read {} has a negative score increase of {} on graph {} vs. {}'.format(
                                read_name, score_diff, key, compare_against))
                
                    if not baseline_dict.has_key(key):
                        # We might get new graphs that aren't in the baseline file.
                        log.warning('Key {} missing from score baseline dict for {}. Inserting...'.format(key, compare_against))
                        # Store 0 for the read count, and 1 for the portion that got worse.
                        # We need a conservative default baseline so new tests will pass.
                        baseline_dict[key] = [0, 1]
                    
                    # Report on its stats after dumping reads, so that if there are
                    # too many bad reads and the stats are terrible we still can see
                    # the reads.
                    print '{} vs. {} Worse: {} Baseline: {}  Threshold: {}'.format(
                        key, compare_against, score_stats_dict[key][1], baseline_dict[key][1], self.worse_threshold)
                    # Make sure all the reads came through
                    self.assertEqual(score_stats_dict[key][0], reads)
                    
                    if not key.endswith('-pe'):
                        # Skip paired-end cases because their pair partners can
                        # pull them around. Also they are currently subject to
                        # substantial nondeterministic alignment differences
                        # based on the assignment of reads to threads.
                    
                        # Make sure not too many got worse
                        self.assertLessEqual(score_stats_dict[key][1], baseline_dict[key][1] + self.worse_threshold)

            
    def _test_mapeval(self, reads, region, baseline_graph, test_graphs, score_baseline_graph=None,
                      positive_control=None, negative_control=None, sample=None,
                      source_path_names=set(), multipath=False, paired_only=False,
                      assembly="hg38", tag_ext="", acc_threshold=0, auc_threshold=0,
                      sim_opts='-l 150 -p 500 -v 50 -e 0.05 -i 0.01', sim_fastq=None):
        """ Run simulation on a bakeoff graph
        
        Simulate the given number of reads from the given baseline_graph
        (snp1kg, primary, etc.) and realign them against all the graphs in the
        test_graph list.
        
        If a sample is specified, baseline_graph must be a graph with allele
        paths in it (--alt_paths passed to toil-vg construct) so that the subset
        of the graph for that sample can be used for read simulation.
        
        If instead source_path_names is specified, it must be a collection of
        path names that exist in baseline_graph. Reads will be simulated evenly
        across the named paths (not weighted according to path length).
        
        Needs to know the bekeoff region that is being run, in order to look up
        the actual graphs files for each graph type.
        
        Verifies that the realignments are sufficiently good.
        
        If score_baseline_graph is set to a graph name from test_graphs,
        computes score differences for reach read against that baseline.

        If postive_control or negative_control in tests_graphs, compute separate
        ROC/QQ plots with just those and the baseline graph (and don't plot them
        in the normal plots)

        If a sample name is specified, extract a thread for each of its haplotype
        from the baseline graph using the gpbwt and simulate only from the threads
        
        """
        assert not tag_ext or tag_ext.startswith('-')
        tag = 'sim-{}-{}{}'.format(region, baseline_graph, tag_ext)
        
        # compute the xg indexes from scratch
        for graph in set([baseline_graph] + test_graphs):
            chrom, offset = self._bakeoff_coords(region)        
            vg_path = self._input('{}-{}.vg'.format(graph, region))
            self._toil_vg_index(str(chrom), vg_path, None, self._input('{}-{}.gcsa'.format(graph, region)),
                                None, tag, '{}-{}'.format(graph, region))

        # compute the haplotype graphs to simulate from
        if sample:
            # Can't use source paths with a sample
            assert(len(source_path_names) == 0)
            
            # We need to make one XG per sample haplotype
            if sample in self.bakeoff_removed_samples:
                # Unlike the other bakeoff graphs (snp1kg-region.vg), this one contains NA12878 and family
                vg_path = self._input('{}_all_samples-{}.vg'.format(baseline_graph, region))
            else:
                vg_path = self._input('{}-{}.vg'.format(baseline_graph, region))
            vcf_path = self._input('1kg_{}-{}.vcf.gz'.format(assembly, region))            
            xg_path, thread1_xg_path, thread2_xg_path = self._make_thread_indexes(
                sample, vg_path, vcf_path, region, tag)
            sim_xg_paths = [thread1_xg_path, thread2_xg_path]
        else:
            # Just use the one XG, and maybe restrict to paths in it.
            xg_path = os.path.join(self._outstore(tag), '{}-{}'.format(baseline_graph, region) + '.xg')
            sim_xg_paths = [xg_path]            
            
        fasta_path = self._input('{}.fa'.format(region))
        test_index_bases = []
        for test_graph in test_graphs:
            test_tag = '{}-{}'.format(test_graph, region)
            test_index_bases.append(os.path.join(self._outstore(tag), test_tag))
        self._mapeval_vg_run(reads, xg_path, sim_xg_paths, source_path_names, fasta_path, test_index_bases,
                             test_graphs, score_baseline_graph, multipath, paired_only, sim_opts, sim_fastq, tag)
        if self.verify:
            self._verify_mapeval(reads, baseline_graph, score_baseline_graph,
                                 positive_control, negative_control, tag,
                                 acc_threshold, auc_threshold)

    @skip("skipping test to keep runtime down")
    @timeout_decorator.timeout(3600)
    def test_sim_brca1_snp1kg(self):
        """ Mapping and calling bakeoff F1 test for BRCA1 primary graph """
        # Using 100k simulated reads from snp1kg BRCA1, realign against all
        # these other BRCA1 graphs and make sure the realignments are
        # sufficiently good. Compare all realignment scores agaisnt the scores
        # for the primary graph.
        log.info("Test start at {}".format(datetime.now()))
        self._test_mapeval(100000, 'BRCA1', 'snp1kg',
                           ['primary', 'snp1kg'],
                           score_baseline_graph='primary',
                           sample='HG00096', acc_threshold=0.02, auc_threshold=0.02)
                           
    @timeout_decorator.timeout(3600)
    def test_sim_mhc_cactus(self):
        """ Mapping test for MHC cactus graph """
        log.info("Test start at {}".format(datetime.now()))        
        self._test_mapeval(10000, 'MHC', 'cactus',
                           ['snp1kg', 'cactus'],
                           multipath=True,
                           source_path_names=['GI568335986', 'GI568335994'], acc_threshold=0.02, auc_threshold=0.02)

    @timeout_decorator.timeout(16000)        
    def test_sim_chr21_snp1kg(self):
        log.info("Test start at {}".format(datetime.now()))
        self._test_mapeval(300000, 'CHR21', 'snp1kg',
                           ['primary', 'snp1kg', 'thresholded10'],
                           score_baseline_graph='primary',
                           sample='HG00096',
                           assembly="hg19",
                           acc_threshold=0.0075, auc_threshold=0.075, multipath=True,
                           sim_opts='-l 150 -p 500 -v 50 -e 0.01 -i 0.002')

    @timeout_decorator.timeout(16000)        
    def test_sim_chr21_snp1kg_trained(self):
        self._test_mapeval(100000, 'CHR21', 'snp1kg',
                           ['primary', 'snp1kg'],
                           #score_baseline_graph='primary',
                           sample='HG00096',
                           assembly="hg19",
                           acc_threshold=0.0075, auc_threshold=0.075, multipath=True, paired_only=True,
                           tag_ext='-trained',
                           sim_opts='-p 500 -v 50 -S 4 -i 0.002',
                           # 800k 148bp reads from Genome in a Bottle NA12878 library
                           # (placeholder while finding something better)
                           sim_fastq='ftp://ftp-trace.ncbi.nlm.nih.gov/giab/ftp/data/NA12878/NIST_NA12878_HG001_HiSeq_300x/131219_D00360_005_BH814YADXX/Project_RM8398/Sample_U5a/U5a_AGTCAA_L002_R1_007.fastq.gz')

    @skip("skipping test to keep runtime down")        
    @timeout_decorator.timeout(3600)
    def test_sim_brca2_snp1kg_mpmap(self):
        """ multipath mapper test, which is a smaller version of above.  we catch all errors
        so jenkins doesn't report failures.  vg is run only in single ended with multipath on
        and off. 
        """
        log.info("Test start at {}".format(datetime.now()))
        self._test_mapeval(50000, 'BRCA2', 'snp1kg',
                           ['primary', 'snp1kg'],
                           score_baseline_graph='primary',
                           sample='HG00096', multipath=True, tag_ext='-mpmap',
                           acc_threshold=0.02, auc_threshold=0.02)

    @skip("skipping test to keep runtime down")        
    @timeout_decorator.timeout(7200)
    def test_sim_chr21_snp1kg_mpmap(self):
        """ multipath mapper test, which is a smaller version of above.  we catch all errors
        so jenkins doesn't report failures.  vg is run only in single ended with multipath on
        and off.
        """
        self._test_mapeval(100000, 'CHR21', 'snp1kg',
                           ['primary', 'snp1kg'],
                           score_baseline_graph='primary',
                           sample='HG00096', multipath=True, tag_ext='-mpmap',
                           acc_threshold=0.02,
                           sim_opts='-d 0.01 -p 1000 -v 75.0 -S 5',
                           sim_fastq=self._input('platinum_NA12878_MHC.fq.gz'))

    @timeout_decorator.timeout(7200)
    def test_sim_mhc_snp1kg_mpmap(self):
        """ multipath mapper test, which is a smaller version of above.  we catch all errors
        so jenkins doesn't report failures.  vg is run only in single ended with multipath on
        and off.
        """
        log.info("Test start at {}".format(datetime.now()))
        self._test_mapeval(50000, 'MHC', 'snp1kg',
                           ['primary', 'snp1kg'],
                           score_baseline_graph='primary',
                           sample='HG00096', multipath=True, tag_ext='-mpmap',
                           acc_threshold=0.02, auc_threshold=0.02,
                           sim_opts='-d 0.01 -p 1000 -v 75.0 -S 5',
                           sim_fastq=self._input('platinum_NA12878_MHC.fq.gz'))

    @timeout_decorator.timeout(7200)
    def test_sim_yeast_cactus(self):
        """ Yeast test based on the cactus graphs.  Reads are simulated from the SK1 path
        of the full graph.  The other graphs are made from this graph using vg mod:
        cactus_drop_SK1 : remove all elements that are only on SK1 path
        cactus_SK1 : keep only SK1 path
        cactus_S288c : keep only S288c (reference) path
        """
        self.input_store = 'https://cgl-pipeline-inputs.s3.amazonaws.com/vg_cgl/cactus_yeast'
        log.info("Test start at {}".format(datetime.now()))
        self._test_mapeval(100000, 'YEAST', 'cactus',
                           ['cactus', 'cactus_drop_SK1', 'cactus_SK1', 'cactus_S288c'],
                           #score_baseline_graph='cactus_S288c',
                           source_path_names=['SK1.chr{}'.format(i) for i in [
                               'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII',
                               'IX', 'X', 'XI', 'XII', 'XIII', 'XIV', 'XV', 'XVI']],
                           #multipath=True,
                           #paired_only=True,
                           acc_threshold=0.02, auc_threshold=0.02,
                           sim_opts='-p 500 -v 50 -S 4 -i 0.002')
    
    @timeout_decorator.timeout(200)
    def test_map_brca1_primary(self):
        """ Mapping and calling bakeoff F1 test for BRCA1 primary graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA1', 'primary', True)

    @timeout_decorator.timeout(200)        
    def test_map_brca1_snp1kg(self):
        """ Mapping and calling bakeoff F1 test for BRCA1 snp1kg graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA1', 'snp1kg', True)
        
    @timeout_decorator.timeout(600)
    def test_map_brca1_snp1kg_mpmap(self):
        """ Mapping and calling bakeoff F1 test for BRCA1 snp1kg graph on mpmap.  
        The filter_opts are the defaults minus the identity filter because mpmap doesn't 
        write identities.
        """
        self._test_bakeoff('BRCA1', 'snp1kg', True, multipath=True, tag_ext='-mpmap',
                           misc_opts='--filter_opts \"-q 15 -m 1 -D 999 -s 1000\"')

    @timeout_decorator.timeout(200)        
    def test_map_brca1_cactus(self):
        """ Mapping and calling bakeoff F1 test for BRCA1 cactus graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA1', 'cactus', True)

    @timeout_decorator.timeout(900)        
    def test_full_brca2_primary(self):
        """ Indexing, mapping and calling bakeoff F1 test for BRCA2 primary graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA2', 'primary', False)

    @timeout_decorator.timeout(900)        
    def test_full_brca2_snp1kg(self):
        """ Indexing, mapping and calling bakeoff F1 test for BRCA2 snp1kg graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA2', 'snp1kg', False)

    @timeout_decorator.timeout(900)        
    def test_full_brca2_cactus(self):
        """ Indexing, mapping and calling bakeoff F1 test for BRCA2 cactus graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('BRCA2', 'cactus', False)

    @skip("skipping test to keep runtime down")
    @timeout_decorator.timeout(2000)        
    def test_map_sma_primary(self):
        """ Indexing, mapping and calling bakeoff F1 test for SMA primary graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('SMA', 'primary', True)

    @skip("skipping test to keep runtime down")        
    @timeout_decorator.timeout(2000)        
    def test_map_sma_snp1kg(self):
        """ Indexing, mapping and calling bakeoff F1 test for SMA snp1kg graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('SMA', 'snp1kg', True)

    @skip("skipping test to keep runtime down")        
    @timeout_decorator.timeout(2000)        
    def test_map_sma_cactus(self):
        """ Indexing, mapping and calling bakeoff F1 test for SMA cactus graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('SMA', 'cactus', True)

    @skip("skipping test to keep runtime down")         
    @timeout_decorator.timeout(2000)        
    def test_map_lrc_kir_primary(self):
        """ Indexing, mapping and calling bakeoff F1 test for LRC-KIR primary graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('LRC-KIR', 'primary', True)

    @skip("skipping test to keep runtime down")         
    @timeout_decorator.timeout(2000)        
    def test_map_lrc_kir_snp1kg(self):
        """ Indexing, mapping and calling bakeoff F1 test for LRC-KIR snp1kg graph """
        self._test_bakeoff('LRC-KIR', 'snp1kg', True)
        
    @skip("skipping test to keep runtime down")         
    @timeout_decorator.timeout(2000)        
    def test_map_lrc_kir_cactus(self):
        """ Indexing, mapping and calling bakeoff F1 test for LRC-KIR cactus graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('LRC-KIR', 'cactus', True)

    @timeout_decorator.timeout(10000)        
    def test_map_mhc_primary(self):
        """ Indexing, mapping and calling bakeoff F1 test for MHC primary graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('MHC', 'primary', True)

    @timeout_decorator.timeout(10000)        
    def test_map_mhc_snp1kg(self):
        """ Indexing, mapping and calling bakeoff F1 test for MHC snp1kg graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('MHC', 'snp1kg', True)

    @skip("skipping test to keep runtime down (baseline missing as well)")          
    @timeout_decorator.timeout(10000)        
    def test_map_mhc_cactus(self):
        """ Indexing, mapping and calling bakeoff F1 test for MHC cactus graph """
        log.info("Test start at {}".format(datetime.now()))
        self._test_bakeoff('MHC', 'cactus', True)
