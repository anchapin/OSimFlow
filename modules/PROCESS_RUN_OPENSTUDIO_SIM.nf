// =============================================================================
// PROCESS_RUN_OPENSTUDIO_SIM.nf
// =============================================================================
// The core simulation engine. Receives a per-sample modified package and runs
// `openstudio.cli run -w workflow.osw` inside the dynamically-selected
// `openstudio_cli_image:<version>` container.
//
// PRD reference: §4.2 — PROCESS_RUN_OPENSTUDIO_SIM
//
//   Inputs:  tuple(sample_id, modified_sim_package_dir)
//            val(openstudio_version)
//   Output:  tuple(sample_id, simulation_output_dir)
//
// Intermediate-file optimization (PRD §1.4): on successful exit, delete the
// large eplusout.err to keep the work directory small.
// =============================================================================

process RUN_OPENSTUDIO_SIM {

    tag    "$sample_id | OS ${openstudio_version}"
    label  'process_high'

    // The container tag is dynamic — driven by --openstudio_version.
    container { "ghcr.io/anchapin/openstudio_cli_image:${openstudio_version}" }

    // PRD §5.2: provide more cores/memory for the actual simulation process.
    cpus   4
    memory '8 GB'
    time   '4h'

    input:
    tuple val(sample_id), path(modified_sim_package)
    val   openstudio_version

    output:
    tuple val(sample_id), path("out"), emit: results
    path  "logs/eplusout.err"        , emit: err_log
    path  "logs/eplusout.log"        , emit: run_log
    path  "out/eplusout.sql"         , emit: sql, optional: true

    script:
    def archive = params.archive_intermediates ? '--archive' : ''
    """
    set -euo pipefail
    mkdir -p out logs

    # Run the OpenStudio workflow.
    openstudio.cli run -w ${modified_sim_package}/workflow.osw \\
        --debug \\
        > logs/stdout.log 2> logs/stderr.log

    # Intermediate-file optimization: drop the large .err on success.
    if [ -f out/eplusout.err ] && [ ! -s out/eplusout.err ]; then
        rm -f out/eplusout.err
    fi
    """

    stub:
    """
    mkdir -p out logs
    echo "stub OpenStudio run for sample=${sample_id} version=${openstudio_version}" > logs/stdout.log
    """
}
