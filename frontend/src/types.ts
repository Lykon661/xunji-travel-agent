export interface Requirements {
  origin: string | null; cities: string[]; start_date: string | null; end_date: string | null;
  people: number | null; rooms: number | null; budget: number | null; preferences: string;
  transport: 'any' | 'train' | 'flight'; bed_type: string; return_to_origin: boolean; self_arranged_round_trip: boolean;
  local_transport: 'public' | 'walking' | 'driving' | 'bicycling';
  origin_address?: string; children?: number; children_ages?: number[];
  earliest_departure?: string | null; latest_arrival?: string | null; latest_return?: string | null;
  max_transport_hours?: number | null; direct_only?: boolean; flight_cabin?: string; train_seat?: string;
  hotel_type?: string; hotel_stars?: string; hotel_areas?: Record<string,string>; max_walk_per_day?: number; allow_taxi?: boolean;
  required_stop_ids?: string[]; excluded_stop_ids?: string[];
  walking_limit_exempt_dates?: string[];
  repeat_exceptions?: {place_id: string; role: 'visit' | 'dining'; dates: string[]; meals: ('lunch' | 'dinner')[]; authorization: string}[];
  constraints?: PlanningConstraint[]; constraints_basis?: string;
}
export interface PlanningConstraint {
  id: string; kind: 'fewer_walking' | 'fixed_order' | 'time_anchor' | 'visit_duration' | 'transfer_buffer' | 'booking_prerequisite' | 'transit_rule' | 'daily_min_stops';
  strength: 'hard' | 'soft'; scope: {dates: string[]; stop_ids: string[]; cities: string[]};
  before: string; after: string; anchor: '' | 'sunrise' | 'sunset' | 'appointment'; at: string | null; minutes: number | null; meters: number | null; count: number | null;
  mode: '' | 'train' | 'flight' | 'ferry'; rule: '' | 'public_only' | 'auto_by_transit'; revoke: boolean;
  quote: string; source: 'form' | 'message' | 'interview' | 'decision'; round_id: string; superseded_by: string;
}
export interface Evidence { kind: 'text' | 'image'; quote: string; image_index: number | null }
export interface GuideStop {id: string; name: string; city: string; day: number; evidence: Evidence; category?: 'sight' | 'dining'; ticket_claim?: string | null}
export interface Source {
  id: string; title: string; text: string; url: string; images: string[]; city_hint: string;
  imported: boolean; kind: string; likes: number; favorites: number; published_at: string | number | null;
  collected_at: string; error?: string | null;
  extraction?: {title: string; stops: GuideStop[]; gaps: string[]; cautions: string[]; audience?: string; transport?: string; seasons: string[]} | null;
}
export interface Candidate {
  id: string; title: string; reason: string; gaps: string[];
  days: {date: string; city: string; stop_ids: string[]; adjustment: string}[];
  draft?: Itinerary; draft_revision?: number; plan_hash?: string; input_hash?: string; confirmation_blockers?: string[]; review?: ReviewState | null;
  feasibility?: {queried_at: string; budget: Itinerary['budget']; issues: Itinerary['issues']; information_unknown: string[];
    transport: {label: string; selected: Partial<Resource>; reason: string}[];
    hotels: {city: string; selected_by_user?: boolean; name?: string | null; area: string[]; commute_minutes?: number | null; selected?: Partial<Resource>; reason?: string; errors?: string[]; query_status?: string}[];
    days: {date: string; city: string; available_start: number; available_end: number; travel_minutes?: number; gaps: string[]}[]};
}
export interface InterviewQuestion {
  id: string; title: string; question: string; options: string[]; recommendation: string;
}
export interface InterviewResponse {
  round_id: string; answers: Record<string,string>; note: string; confirm: boolean;
  draft_revision?: number;
}
export interface Interview {
  summary: string; questions: InterviewQuestion[]; round: number; round_id: string; confirmed: boolean;
  history: {questions: InterviewQuestion[]; summary: string; answers: Record<string,string>; note: string}[];
}
export interface Pending {
  kind: string; message: string; missing?: string[];
  questions?: InterviewQuestion[]; summary?: string; round?: number; round_id?: string;
  draft_revision?: number; origin_step?: string; resume_stage?: string;
}
export interface Resource {
  id: string; name: string; kind: string; price: number | null; price_basis: string; currency: string;
  queried_at: string; provider: string; booking_url: string | null; departure: string | null; arrival: string | null;
  reference_unit?: string | null;
  context?: Record<string,string>;
  price_date?: string | null; ticket_name?: string | null; price_note?: string; assumed_free?: boolean; reported_free?: boolean;
  supplier_id?: string | null; image_url?: string | null; platform_hint?: string; seat_class?: string | null;
  departure_place?: string | null; arrival_place?: string | null; departure_terminal?: string | null; arrival_terminal?: string | null;
  rating?: string | number | null; star?: string | null; brand?: string | null; bed_type?: string | null;
  address?: string | null; cancellation?: string | null; breakfast?: string | null;
  door_minutes?: number | null; commute_minutes?: number | null; walking_meters?: number;
  constraint_issues?: string[]; comparison_unknown?: string[]; map_place?: MapPlace | null;
  departure_transfer?: Movement; arrival_transfer?: Movement;
}
export interface MapPlace {id?: string | null; name: string; location?: string | null; address?: string | null; map_url?: string}
export interface Product extends Resource {city: string; search_mode: string; reference_only: boolean; match_basis?: string}
export interface MapResult {links: string[]; app_uri?: string | null; web_url?: string | null; status?: string; skipped?: string[]; note?: string; queried_at?: string}
export interface ResourceGroup {
  id: string; label: string; city?: string; context: Record<string,string>;
  selected: Resource | null; options: Resource[]; reason: string; errors: string[];
  query_status?: string;
  search_areas?: string[];
  selected_by_user?: boolean;
}
export interface Movement {
  origin?: string; destination?: string;
  minutes: number | null; estimate_minutes?: number; note?: string; mode?: string; queried_at?: string;
  walking_meters?: number | null; distance_meters?: number | null; summary?: string; allowed_modes?: string[];
  cost?: number | null;
  navigation_url?: string | null; origin_place?: MapPlace; destination_place?: MapPlace;
  evidence_id?: string | null;
  failure_codes?: string[]; refresh_failed?: boolean; refresh_note?: string; walking_alternative?: Movement;
  transport_label?: string;
}
export interface Weather {
  date: string; available: boolean; dayweather?: string; nightweather?: string;
  daytemp?: string | number | null; nighttemp?: string | number | null; note?: string;
  reporttime?: string | null; queried_at?: string | null;
}
export interface Activity {
  id: string; kind: 'visit' | 'rest'; name: string; start: string; end: string;
  duration_basis?: string; source_id?: string; source_title?: string; source_url?: string | null;
  evidence?: Evidence; move_before?: Movement; opening_claim?: string; ticket_claim?: string;
  ticket_query_required?: boolean;
  map_url?: string; adjustment?: string;
  category?: 'sight' | 'dining';
  meal?: 'lunch'; visit_minutes?: number;
  meal_break?: {start: string; end: string; duration_minutes: number; adjustment: string};
  canonical_place_id?: string; alias_names?: string[];
  source_references?: {stop_id: string; source_id?: string; source_title?: string; evidence?: Evidence}[];
  map_place?: MapPlace; navigation_url?: string | null; source_kind?: string; product?: Product;
  nearby?: MapPlace[]; nearby_note?: string;
  ticket_quote?: {price: number | null; assumed_free?: boolean; reported_free?: boolean; price_date?: string | null; visit_date?: string; ticket_name?: string | null; note?: string; booking_url?: string | null; queried_at?: string; status?: string; candidates?: Resource[]; options?: Resource[]};
}
export interface MealRecommendation {
  id: string; name: string; meal: 'lunch' | 'dinner'; nearby_activity: string; distance_meters: number;
  note: string; map_place: MapPlace; map_url?: string; opening_claim?: string | null;
  source_id?: string; source_title?: string; source_url?: string | null; evidence?: Evidence;
  canonical_place_id?: string;
  source_references?: {stop_id: string; source_id?: string; source_title?: string; evidence?: Evidence}[];
}
export interface ReviewState {
  status: 'pass' | 'pass_with_warnings' | 'revise' | 'needs_evidence' | 'needs_decision' | 'review_unavailable';
  summary: string; reviewed_at?: string; expires_at?: string; current?: boolean; review_id?: string;
  checks: {dimension: string; result: string; note: string}[];
  findings: {code: string; severity: 'warning' | 'blocker'; owner: string; action: string; target: string;
    dates: string[]; field_path: string; message: string; evidence_ids: string[]; suggestion: string}[];
}
export interface RouteChatMessage {role: 'user' | 'assistant'; content: string; time: string; dates?: string[];selected_names?:string[]}
export interface Itinerary {
  recovery_note?: string;
  days: {date: string; city: string; activities: Activity[]; gaps: string[]; adjustment: string; return_move: Movement;
    weather?: Weather; route_basis?: string; travel_minutes?: number; travel_estimated?: boolean;
    walking_meters?: number; meal_recommendations?: MealRecommendation[];
    end_label?: string; return_start?: string; return_end?: string;
    transfers?: {label: string; move: Movement}[]; transport_transfers?: {label: string; move: Movement}[];
    meal_estimate: number; meal_basis: string}[];
  transport: ResourceGroup[]; hotels: ResourceGroup[];
  personal_map?: MapResult; change_summary?: string[];
  information_unknown?: string[];
  constraint_notes?: string[];
  issues: {code: string; message: string; blocking: boolean; target: string}[];
  budget: {known_subtotal: number; estimated_meals: number; known_plus_estimates: number; limit: number;
    estimated_resources?: number; estimated_tickets?: number; estimated_local_transport?: number;
    status: string; unknown: string[]; lines: {label: string; amount: number | null; estimated_amount?: number | null; basis: string; kind: string}[]};
}
export interface TripDocument {
  rollback?: {reason: string; time: string; message: string};
  requirements: Requirements; stage: string; progress_stage?: string; progress_message?: string; workflow_step?: string; candidates: Candidate[]; selected_route?: Candidate;
  selected_source_ids?: string[];
  interview?: Interview | null;
  itinerary: Itinerary | null; warnings: string[]; locks: string[];
  review?: ReviewState | null;
  itinerary_outdated?: boolean;
  itinerary_saved_version?: number | null;
  messages: {role: string; content: string; time: string}[];
  pending: Pending | null;
  changes?: {explanation?: string};
  sources_snapshot?: Source[];
  products?: Product[]; reference_product_ids?: string[];
  product_search?: {query: string; mode: string; city?: string; min_price?: number | null; max_price?: number | null; hotel_location?: 'route' | 'custom' | 'city'; hotel_area?: string; summary?: string; platform_hint?: string; message: string; status?: string; code?: string; queried_at?: string; result_ids?: string[]};
  supplements?: (GuideStop & {source_kind: string; assigned_date: string})[];
  draft_revision?: number; stop_constraints?: {required: string[]; excluded: string[]};
  decision_history?: Interview['history'];
  route_chat?: RouteChatMessage[];
  confirmed_plan?: {draft_revision: number; plan_hash: string; confirmed_at: string};
}
export interface Run {id: string; status: string; document: {message?: string; code?: string}}
export interface Trip {id: string; title: string; version: number; updated_at: string; document: TripDocument; sources: Source[]; active_run: string | null; run?: Run}
export interface TripSummary {id: string; title: string; stage: string; version: number; updated_at: string; active_run: string | null}
export interface Version {version: number; document: TripDocument; created_at: string}
export type VersionSummary = Pick<Version,'version' | 'created_at'>
export interface ProgressEvent {id: number; kind: string; message: string; time: string; stage?: string}

export type ModelApi = 'openai-completions' | 'openai-responses' | 'openai-codex-responses' | 'anthropic-messages' | 'google-generative-ai' | 'mistral-conversations'
export type ThinkingLevel = 'default' | 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export interface ModelConfiguration {
  provider: string; api: ModelApi; base_url: string; model: string; allow_without_key: boolean
  timeout_seconds: number; context_window: number; max_tokens: number; reasoning: boolean; supports_images: boolean
  has_api_key: boolean; configured: boolean; source: 'none' | 'environment' | 'saved'
  provider_id?: string; provider_name?: string; model_name?: string; credential_source?: string
}
export interface CatalogModel {
  id: string; name: string; api: ModelApi; base_url: string; context_window: number; max_tokens: number; reasoning: boolean; supports_images: boolean
  thinking_levels?: Exclude<ThinkingLevel, 'default'>[]; compat?: Record<string, boolean | string>; cost?: Record<string, number>
}
export interface CatalogProvider {
  id: string; name: string; base_url: string; env_key: string | null; models: CatalogModel[]
  supports_oauth: boolean; subscription: boolean; oauth_name: string; oauth_label: string
}
export interface ManagedModel {
  id: string; name: string; api: ModelApi; context_window: number; max_tokens: number
  reasoning: boolean; supports_images: boolean; thinking_level: ThinkingLevel; temperature: number | null
  compat: Record<string, boolean | string>
}
export interface ManagedProvider {
  id: string; name: string; provider: string; base_url: string; timeout_seconds: number
  credential_mode: 'api_key' | 'environment' | 'none' | 'oauth'; has_api_key: boolean; has_headers: boolean
  supports_oauth: boolean; oauth_logged_in: boolean
  env_key: string | null; credential_source: string; configured: boolean; models: ManagedModel[]
}
export interface ModelSelection { provider_id: string; model_id: string }
export interface ModelRegistry {
  version: number; providers: ManagedProvider[]; active: ModelSelection | null; vision?: ModelSelection | null; vision_current: ModelConfiguration; auto_select?: boolean
  current: ModelConfiguration; source: ModelConfiguration['source']
}
export interface ModelOAuthSession {
  id: string; provider_id: string; provider_name: string
  status: 'pending' | 'waiting_input' | 'connected' | 'failed' | 'cancelled'; message: string
  events: { type: 'auth_url' | 'device_code' | 'progress' | 'info'; url?: string; instructions?: string
    userCode?: string; verificationUri?: string; message?: string; links?: { url: string; label?: string }[] }[]
  prompt: { id: string; type: 'text' | 'secret' | 'select' | 'manual_code'; message: string; placeholder?: string
    options?: { id: string; label: string; description?: string }[] } | null
}

export interface Capabilities {
  model_configured: boolean; model_active: boolean; model_name: string;
  crawler_enabled: boolean; crawler_revision: string; flyai_enabled: boolean; flyai_configured: boolean; flyai_ready: boolean; flyai_message: string; amap_configured: boolean;
}
export interface CrawlerState {
  installed: boolean; present: boolean; enabled: boolean; busy: boolean; collecting: boolean;
  blocked: boolean; cooldown_seconds: number;
  stage: 'idle' | 'installing' | 'logging_in' | 'ready' | 'failed'; message: string;
}
