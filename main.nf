// =============================================================================
// main.nf — OSimFlow top-level workflow (skeleton)
// =============================================================================
// Status: stub. See docs/OSimFlow.md §4.2 for the canonical process definitions
// and §5.2 for the MVP delivery scope.
//
// Target shape once implemented:
//
//   variables.yml ──> GENERATE_LHS_SAMPLES ──┐
//                                            ├──> APPLY_PARAMETERS ──> RUN_OPENSTUDIO_SIM ──┐
//                                                                                            ├──> EXTRACT_KPIS ──┐
//                                                                                            │                   ├──> AGGREGATE_RESULTS ──> GENERATE_BASIC_PLOTS
//                                                                                            └───────────────────┘
//
// All six processes live in modules/PROCESS_*.nf. This file is the orchestrator
// only — no process bodies here.
// =============================================================================

nextflow.enable.dsl = 2

// -----------------------------------------------------------------------------
// Parameters (CLI surface). All flags documented in AGENTS.md §4.
// -----------------------------------------------------------------------------
params.input_variables        = "${projectDir}/variables.yml"
params.template_sim_package   = "${projectDir}/example_package"
params.n_samples              = 10
params.outdir                 = "results"
params.openstudio_version     = "3.4.0"
params.archive_intermediates  = false

// BYOS (Bring Your Own Script) overrides — see user_scripts/README.md.
params.custom_apply_script    = null
params.custom_kpi_extractor   = null

// -----------------------------------------------------------------------------
// Process includes.
// -----------------------------------------------------------------------------
include { GENERATE_LHS_SAMPLES    } from './modules/PROCESS_GENERATE_LHS_SAMPLES.nf'
include { APPLY_PARAMETERS        } from './modules/PROCESS_APPLY_PARAMETERS.nf'
include { RUN_OPENSTUDIO_SIM      } from './modules/PROCESS_RUN_OPENSTUDIO_SIM.nf'
include { EXTRACT_KPIS            } from './modules/PROCESS_EXTRACT_KPIS.nf'
include { AGGREGATE_RESULTS       } from './modules/PROCESS_AGGREGATE_RESULTS.nf'
include { GENERATE_BASIC_PLOTS    } from './modules/PROCESS_GENERATE_BASIC_PLOTS.nf'

// -----------------------------------------------------------------------------
// Main workflow.
// -----------------------------------------------------------------------------
workflow {

    // TODO(impl): wire the six processes together per docs/OSimFlow.md §4.2.
    //
    // Pseudocode for the implementer:
    //
    //   samples_ch     = GENERATE_LHS_SAMPLES(params.input_variables, params.n_samples)
    //   parameterized  = APPLY_PARAMETERS(samples_ch, params.template_sim_package, params.custom_apply_script)
    //   simulations    = RUN_OPENSTUDIO_SIM(parameterized, params.openstudio_version, params.archive_intermediates)
    //   kpis           = EXTRACT_KPIS(simulations, params.custom_kpi_extractor)
    //   aggregated     = AGGREGATE_RESULTS(kpis, simulations.log_dirs)
    //   GENERATE_BASIC_PLOTS(aggregated.results_csv, aggregated.failed_csv)
}
