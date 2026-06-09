// =============================================================================
// PROCESS_EXTRACT_KPIS.nf
// =============================================================================
// Parses one sample's `eplusout.sql` (and other output artifacts) into a
// structured JSON of KPIs. Defaults to bin/extract_kpis.py; supports a
// user-supplied custom_kpi_extractor (BYOS) via --custom_kpi_extractor.
//
// PRD reference: §4.2 — PROCESS_EXTRACT_KPIS
//
//   Inputs:  tuple(sample_id, simulation_output_dir)
//            val(custom_kpi_extractor)  optional BYOS override
//   Output:  tuple(sample_id, kpi_json_file)
// =============================================================================

process EXTRACT_KPIS {

    tag    "$sample_id"
    label  'process_low'

    container 'ghcr.io/anchapin/scientific_python_image:latest'

    publishDir "${params.outdir}/kpis", mode: 'copy', overwrite: true, pattern: '*.json'

    input:
    tuple val(sample_id), path(simulation_output_dir)
    val   custom_kpi_extractor   // path string or null

    output:
    tuple val(sample_id), path("kpi_${sample_id}.json"), emit: kpi

    script:
    def byos = custom_kpi_extractor ? "--custom_kpi_extractor ${custom_kpi_extractor}" : ''
    """
    python ${projectDir}/bin/extract_kpis.py \\
        --simulation_dir ${simulation_output_dir} \\
        --sample_id      ${sample_id} \\
        --out            kpi_${sample_id}.json \\
        ${byos}
    """

    stub:
    """
    echo '{"sample_id":"${sample_id}","kpis":{}}' > kpi_${sample_id}.json
    """
}
