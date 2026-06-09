// =============================================================================
// PROCESS_APPLY_PARAMETERS.nf
// =============================================================================
// Applies a single (sample_id, parameter_set) tuple to the template_sim_package,
// producing a per-sample modified directory ready for simulation.
//
// PRD reference: §4.2 — PROCESS_APPLY_PARAMETERS
//
//   Inputs:  tuple(sample_id, parameter_set)
//            path(template_sim_package)
//            val(custom_apply_script)  optional BYOS override
//   Output:  tuple(sample_id, modified_sim_package_dir)
//
// Pre-flight check (PRD §1.4): validate that parameters map to real measure
// arguments / .osm attributes BEFORE running the simulation.
// =============================================================================

process APPLY_PARAMETERS {

    tag    "$sample_id"
    label  'process_low'

    container 'ghcr.io/anchapin/scientific_python_image:latest'

    input:
    tuple val(sample_id), path(parameter_set)
    path  template_sim_package
    val   custom_apply_script   // path string or null

    output:
    tuple val(sample_id), path("modified/${sample_id}"), emit: parameterized

    script:
    def byos = custom_apply_script ? "--custom_apply_script ${custom_apply_script}" : ''
    """
    mkdir -p modified
    python ${projectDir}/bin/apply_params_to_model.py \\
        --template      ${template_sim_package} \\
        --parameter_set ${parameter_set} \\
        --sample_id     ${sample_id} \\
        --out           modified/${sample_id} \\
        ${byos}
    """

    stub:
    """
    mkdir -p modified/${sample_id}
    """
}
