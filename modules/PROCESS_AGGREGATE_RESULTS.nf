// =============================================================================
// PROCESS_AGGREGATE_RESULTS.nf
// =============================================================================
// Collects every per-sample KPI JSON and every simulation log directory, then:
//   1. Aggregates KPIs into a single `aggregated_results.csv` (or .parquet).
//   2. Identifies failed samples by inspecting eplusout.err files and writes
//      `failed_simulations.csv` containing the first "Severe Error" line
//      (or other short error summary) per failure.
//
// PRD reference: §4.2 — PROCESS_AGGREGATE_RESULTS
//
//   Inputs:  path(kpi_json_file.collect())
//            path(simulation_output_dir.collect())
//   Output:  path(aggregated_results_csv)
//            path(failed_simulations_csv)
// =============================================================================

process AGGREGATE_RESULTS {

    tag    "all samples"
    label  'process_low'

    container 'ghcr.io/anchapin/scientific_python_image:latest'

    publishDir "${params.outdir}", mode: 'copy', overwrite: true, pattern: '{aggregated_results.csv,failed_simulations.csv,aggregated_results.parquet}'

    input:
    path kpi_json_files
    path simulation_output_dirs

    output:
    path "aggregated_results.csv"   , emit: results_csv
    path "aggregated_results.parquet", emit: results_parquet, optional: true
    path "failed_simulations.csv"   , emit: failed_csv

    script:
    """
    python ${projectDir}/bin/aggregate_results.py \\
        --kpis            ${kpi_json_files} \\
        --simulation_dirs ${simulation_output_dirs} \\
        --out_csv         aggregated_results.csv \\
        --out_parquet     aggregated_results.parquet \\
        --out_failed      failed_simulations.csv
    """

    stub:
    """
    echo 'sample_id,kpi_placeholder' > aggregated_results.csv
    echo 'sample_id,error_summary'   > failed_simulations.csv
    """
}
