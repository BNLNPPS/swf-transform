#!/bin/bash
#
# eicrecon_process.sh — process a single slice from an edm4hep input file
#                       inside the EIC Singularity container.
#
# Placeholders (replaced at runtime by process_payload):
#   {EPIC_VERSION}   — EPIC campaign version, e.g. 26.03.0
#   {INPUT_FILE}     — XRootD or local path of the input edm4hep ROOT file
#   {OUTPUT_FILE}    — path for the eicrecon output edm4eic ROOT file
#   {NSKIP}          — number of events to skip (= start index)
#   {NEVENTS}        — number of events to process (= end - start + 1)
#
set -e

SINGULARITY_IMAGE="/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:{EPIC_VERSION}-stable"

singularity exec "${SINGULARITY_IMAGE}" /bin/bash -c "
set -e

# Initialise the EPIC detector geometry
source /opt/detector/epic-{EPIC_VERSION}/bin/thisepic.sh

# Run reconstruction on the requested event slice
eicrecon \
    {INPUT_FILE} \
    -Ppodio:output_file={OUTPUT_FILE} \
    -Pjana:nevents={NEVENTS} \
    -Pjana:nskip={NSKIP} \
    -Pjana:timeout=0
"
