// =============================================================================
// PROCESS_GENERATE_LHS_SAMPLES.nf
// =============================================================================
// Reads `variables.yml`, validates its structure and types, and produces
// N unique parameter sets using Latin Hypercube Sampling (scipy.stats.qmc).
//
// PRD reference: §4.2 — PROCESS_GENERATE_LHS_SAMPLES
//
//   Inputs:  path(variables_yml)  --input_variables
//            val(n_samples)       --n_samples
//   Output:  tuple(sample_id, parameter_set_dict)  per sample
//   Container: scientific_python_image
// =============================================================================

process GENERATE_LHS_SAMPLES {

    tag    "n=${n_samples}"
    label  'process_low'

    publishDir "${params.outdir}/samples", mode: 'copy', overwrite: true, pattern: 'samples.json'

    container 'ghcr.io/anchapin/scientific_python_image:latest'

    input:
    path variables_yml
    val  n_samples

    output:
    tuple val(sample_id), path(parameter_set), emit: samples
    path 'samples.json'                       , optional: true

    script:
    """
    python ${projectDir}/bin/generate_lhs.py \\
        --variables_yml ${variables_yml} \\
        --n_samples     ${n_samples} \\
        --out           samples.json
    """

    stub:
    """
    mkdir -p samples
    echo '{"samples":[]}' > samples.json
    touch samples.json
    """
}
