// =============================================================================
// PROCESS_GENERATE_BASIC_PLOTS.nf
// =============================================================================
// Renders 1–3 static summary plots from aggregated_results.csv and
// failed_simulations.csv using matplotlib + seaborn.
//
// PRD reference: §4.2 — PROCESS_GENERATE_BASIC_PLOTS
//
// Default plots:
//   1. EUI histogram
//   2. Scatter of top design variable vs. EUI
//   3. Failure-rate bar chart (from failed_simulations.csv)
//
//   Inputs:  path(aggregated_results_csv)
//            path(failed_simulations_csv)
//   Output:  path(kpi_summary_plots_png_pdf)
// =============================================================================

process GENERATE_BASIC_PLOTS {

    tag    "summary plots"
    label  'process_low'

    container 'ghcr.io/anchapin/scientific_python_image:latest'

    publishDir "${params.outdir}/plots", mode: 'copy', overwrite: true, pattern: '*.{png,pdf}'

    input:
    path aggregated_results_csv
    path failed_simulations_csv

    output:
    path "*.png", emit: plots_png
    path "*.pdf", emit: plots_pdf, optional: true

    script:
    """
    python ${projectDir}/bin/generate_plots.py \\
        --results_csv   ${aggregated_results_csv} \\
        --failed_csv    ${failed_simulations_csv} \\
        --outdir        .
    """

    stub:
    """
    mkdir -p .
    echo "stub plot placeholder" > eui_histogram.png
    """
}
