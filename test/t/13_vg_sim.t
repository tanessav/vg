#!/usr/bin/env bash

BASH_TAP_ROOT=../deps/bash-tap
. ../deps/bash-tap/bash-tap-bootstrap

PATH=../bin:$PATH # for vg


plan tests 12

vg construct -r small/x.fa -v small/x.vcf.gz >x.vg
vg index -x x.xg x.vg

is $(vg sim -l 100 -n 100 -x x.xg | wc -l) 100 \
    "vg sim creates the correct number of reads"
    
is $(vg sim -s 1337 -l 100 -n 1 -e 0.0 -i 0.0 -J -x x.xg | jq .score) 110 "end bonuses are included"

is $(vg sim -l 100 -n 100 -a -x x.xg | vg view -a - | wc -l) 100 \
   "alignments may be generated rather than read sequences"

is $(vg sim -s 33232 -l 100 -n 100 -J -x x.xg | grep is_reverse | wc -l) 50 "alignments are produced on both strands"

is $(vg sim -s 1337 -l 100 -n 100 -e 0.1 -i 0.1 -J -x x.xg | jq .sequence | wc -c) 10300 "high simulated error rates do not change the number of bases generated"

is $(vg sim -l 100 -n 100 -x x.xg -aJ | jq 'select(.path.mapping[0].is_reverse)' | wc -l) 0 \
   "vg sim creates forward-strand reads when asked"
   
vg view -j x.vg | jq -r '.path[].mapping[].position.node_id' | sort > path.txt
is $(vg sim -s 1337 -l 100 -n 100 -x x.xg -P "x" -aJ | jq -r '.path.mapping[].position.node_id' | sort | uniq | comm -23 - path.txt | wc -l) "0" "vg sim can simulate from just a path"

is $(vg sim -s 1337 -F reads/grch38_lrc_kir_paired.fq -n 100 -x x.xg -P "x" -aJ | jq -r '.path.mapping[].position.node_id' | sort | uniq | comm -23 - path.txt | wc -l) "0" "vg sim can simulate from just a path in FASTQ mode"

rm -f path.txt

is $(vg sim -n 10 -i 0.005 -l 10 -p 50 -v 50 -s 42 -x x.xg -J | wc -l) 20 "pairs simulated even when fragments overlap"

cat tiny/tiny.fa | sed s/GCTTGGA/GCNTGGA/ >n.fa
vg construct -r n.fa >n.vg
vg index -x n.xg n.vg
is $(vg sim -s 3145 -n 1000 -l 20 -x n.xg | grep N | wc -l) 0 "sim does not emit reads with Ns"

is $(vg sim -s 3145 -N -n 1000 -l 20 -x n.xg | grep N | wc -l) 377 "sim can emit reads with Ns when asked to"

is $(vg sim -s 3145 -n 1000 -l 2 -p 5 -e 0.1 -x n.xg | grep N | wc -l) 0 "sim doesn't emit Ns even with pair and errors"

rm -f x.vg x.xg n.vg n.fa n.xg
