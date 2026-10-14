export type Values = Record<string, unknown>;
export type NodeConfig = { kind: 'local' | 'simulation' | 'ssh'; host?: string; user?: string; port?: number; root?: string; python?: string; key?: string };
export type Product = { id: string; name: string; chip: string; board: string; capabilities: string[]; config: Values; enabled?: boolean; created_at?: string };
export type BuildEnvironment = { id: string; name: string; node: NodeConfig; config: Values; revision?: number };
export type Device = { id: string; name: string; product_id: string; node: NodeConfig; capabilities: string[]; config: Values; health: string };
export type ParameterSchema = {
  type?: string; title?: string; description?: string; default?: unknown; enum?: unknown[];
  minimum?: number; maximum?: number; minLength?: number; maxLength?: number; pattern?: string;
  properties?: Record<string, ParameterSchema>; required?: string[]; items?: ParameterSchema; additionalProperties?: boolean;
};
export type ParameterUi = { label?: string; unit?: string; help?: string; order?: number; group?: string };
export type ManualStep = { id: string; title: string; instruction?: string; expected?: string; pass_criteria?: string; record_schema?: ParameterSchema; fields?: unknown[] };
export type CaseDefinition = {
  parameters_schema?: ParameterSchema; parameter_ui?: Record<string, ParameterUi>; requirements?: { capabilities?: string[] };
  implementation?: Values; effects?: string; test_scope?: string; description?: string; timeout_seconds?: number;
  required_assertions?: string[]; steps?: ManualStep[]; manual_steps?: ManualStep[]; record_schema?: ParameterSchema;
  recovery?: { required?: boolean }; [key: string]: unknown;
};
export type CaseRevision = { id: string; case_id: string; version: string; name: string; mode: 'automated' | 'manual'; definition: CaseDefinition; enabled?: boolean; created_at?: string };
export type CaseBinding = { binding_id: string; case_revision_id: string; required: boolean; parameter_overrides: Values };
export type RepositoryOverride = { path: string; url: string; ref: string };
export type PlanConfig = {
  product_id: string;
  firmware: { kind: 'current_device' | 'artifact' | 'build'; environment_id?: string; bundle_id?: string; expected_version?: string; source?: { kind: 'local_workspace' | 'snapshot'; snapshot_id?: string; repository_overrides: RepositoryOverride[] }; build_only?: boolean };
  device_id?: string; cases: CaseBinding[]; acceptance: { mode: 'all_required_cases' }; policy: Values;
};
export type PlanRevision = { id: string; plan_id: string; version: number; draft_revision?: number; config: PlanConfig; created_at?: string };
export type TestPlan = { id: string; name: string; draft: PlanConfig; revision: number; draft_revision?: number; revisions?: PlanRevision[]; latest_revision?: PlanRevision | null; created_at?: string; updated_at?: string };
export type PlanPreview = { valid: boolean; errors: { path: string; code: string; message: string }[]; warnings: (string | { message: string })[]; parameters: Record<string, Values>; parameter_sources: Record<string, Record<string, string>>; stages: string[]; resolved?: Values };
export type FirmwareBundle = { id: string; name: string; status?: string; product_id?: string; source?: Values; images?: Values[]; created_at?: string; [key: string]: unknown };
export type RunCase = { id?: string; binding_id?: string; name?: string; case_revision_id?: string; mode?: string; state?: string; status?: string; verdict?: string; result?: Values; parameters?: Values; [key: string]: unknown };
export type TestRun = { id: string; plan_id?: string; plan_revision_id?: string; plan_name?: string; name?: string; state?: string; status?: string; stage?: string; phase?: string; verdict?: string; revision: number; message?: string; created_at?: string; started_at?: string; completed_at?: string; cases?: RunCase[]; events?: Values[]; snapshot?: Values; request_snapshot?: Values; result?: Values; operator_requests?: Values[]; [key: string]: unknown };
export type Catalog = { products: Product[]; environments: BuildEnvironment[]; devices: Device[]; cases: CaseRevision[] };
export type View = 'plans' | 'editor' | 'cases' | 'settings' | 'runs' | 'run';
export type Location = { view: View; id?: string; template?: PlanConfig['firmware']['kind'] };

export function emptyPlan(kind: PlanConfig['firmware']['kind'] = 'current_device'): PlanConfig {
  return { product_id: '', firmware: { kind, ...(kind === 'build' ? { source: { kind: 'local_workspace' as const, repository_overrides: [] } } : {}) }, cases: [], acceptance: { mode: 'all_required_cases' }, policy: { independent_case_failure: 'continue', recovery_failure: 'quarantine_device', manual_wait_timeout_seconds: 3600 } };
}
