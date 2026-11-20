export type Mode = 'fixture' | 'live';
export type Strategy = 'fixed' | 'tools' | 'verified';
export type Transport = 'walking' | 'driving' | 'transit';
export type Status = 'queued' | 'running' | 'retrying' | 'succeeded' | 'dead';
export type Stage = 'research' | 'draft' | 'validate' | 'complete';
export type ConfigStatus = 'checking' | 'ready' | 'missing' | 'unreachable';

export interface TripRequest {
  city: string;
  start_date: string;
  days: number;
  travelers: number;
  interests: string[];
  budget: number | null;
  meals_per_person_day?: number;
  transport: Transport;
  hotel: boolean;
  notes?: string;
  pace?: 'relaxed' | 'balanced' | 'packed';
  required_places?: string[];
  avoid_outdoors_in_rain?: boolean;
}

export interface Place {
  poi_id: string;
  name: string;
  address: string;
  lng: number;
  lat: number;
  category?: string;
  evidence?: string;
  official_website?: string;
  pricing?: PlacePricing | null;
  source_url?: string | null;
  source_name?: string | null;
}

export interface PlacePricing {
  status: 'quoted' | 'sample' | 'free' | 'unknown';
  currency?: string;
  amount?: number | null;
  price_kind?: 'from' | 'range' | 'reference';
  admission_group?: string;
  maximum?: number | null;
  nightly?: number | null;
  total?: number | null;
  total_estimated?: boolean;
  nights?: number | null;
  rooms?: number | null;
  source_url?: string | null;
  source_name?: string | null;
  observed_at?: string | null;
  visit_date?: string | null;
  notes?: string[];
  per_night?: {date: string; amount: number}[];
}

export interface BudgetSummary {
  currency: 'CNY';
  limit: number;
  estimated_total: number | null;
  estimated_subtotal: number;
  remaining: number | null;
  status: 'within' | 'over' | 'partial';
  items: {key: string; label: string; amount: number | null; known_amount?: number; unknown_count?: number}[];
  notes: string[];
  provisional_tickets?: number;
  foreign_costs?: Array<{label: string; currency: string; amount: number; maximum?: number | null; price_kind?: 'from' | 'range'}>;
}

export interface Stop extends Place {
  start_time: string;
  end_time: string;
  reason?: string;
}

export interface Day {
  date: string;
  stops: Stop[];
  weather?: Record<string, unknown> | null;
  legs?: Array<Record<string, unknown>>;
}

export interface Forecast {
  date: string;
  summary: string;
  low_c?: number | null;
  high_c?: number | null;
  rain_chance?: number | null;
  source_url?: string | null;
  source_name?: string | null;
  evidence?: string;
}

export interface Guide {
  content: string;
  source: string;
  heading?: string | string[];
  start_line?: number;
  end_line?: number;
}

export interface Itinerary {
  city: string;
  start_date: string;
  days: Day[];
  mode?: Mode;
  hotel: Place | null;
  lodging_search?: {
    source_url: string;
    source_name?: string;
    alternate_url?: string;
    alternate_name?: string;
  } | null;
  warnings: string[];
  candidates?: Place[];
  guides?: Guide[];
  travel_links?: Array<{label: string; source_url: string; source_name: string; kind: string}>;
  budget_summary?: BudgetSummary | null;
  transit_fare_reference?: Array<{label: string; currency: string; amount: number; source_url: string}>;
}

export interface Job {
  id: string;
  status: Status;
  stage: Stage;
  attempts: number;
  max_attempts: number;
  request: TripRequest;
  mode: Mode;
  strategy: Strategy;
  created_at: string;
  updated_at: string;
  error: string | null;
  itinerary: Itinerary | null;
  revision: number;
}

export interface JobEvent {
  id: number;
  type: string;
  stage: Stage;
  message: string;
  created_at: string;
  data: Record<string, unknown>;
}

export const statusLabels: Record<Status, string> = {
  queued: '等待执行', running: '规划中', retrying: '等待重试', succeeded: '已完成', dead: '规划失败',
};
export const stageLabels: Record<Stage, string> = {
  research: '查找地点和资料', draft: '编排每日路线', validate: '校验地点与时间', complete: '行程已就绪',
};
export const transportLabels: Record<Transport, string> = {
  walking: '步行', driving: '驾车', transit: '公共交通',
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError(0, '无法连接规划服务，请检查服务是否运行。');
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail: unknown = payload?.detail;
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map(item => `${item.loc?.at(-1) ?? '表单'}：${item.msg}`).join('；')
        : '操作失败，请稍后重试。';
    throw new ApiError(response.status, message);
  }
  return payload as T;
}

export function getJobs() {
  return api<{ jobs: Job[] }>('/jobs');
}
export function getJob(id: string) {
  return api<Job>(`/jobs/${id}`);
}
export function getForecast(id: string, signal?: AbortSignal) {
  return api<{forecasts: Forecast[]}>(`/jobs/${id}/forecast`, {signal});
}
export function getEvents(id: string) {
  return api<{events: JobEvent[]}>(`/jobs/${id}/events`);
}
export function getConfig() {
  return api<{live_available: boolean; hotel_pricing_available?: boolean; booking_available?: boolean;
    default_meals_per_person_day?: number}>('/config');
}
export function createJob(request: TripRequest, mode: Mode, strategy: Strategy, key: string) {
  return api<Job>('/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Idempotency-Key': key},
    body: JSON.stringify({request, mode, strategy}),
  });
}
export function updateDay(id: string, revision: number, dayIndex: number, ids: string[]) {
  return api<Job>(`/jobs/${id}/itinerary`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({revision, day_index: dayIndex, poi_ids: ids}),
  });
}
export function retryJob(id: string) {
  return api<Job>(`/jobs/${id}/retry`, {method: 'POST'});
}

export const isTerminal = (status?: Status) => status === 'succeeded' || status === 'dead';
export const dateLabel = (date: string) => new Intl.DateTimeFormat('zh-CN', {
  month: 'long', day: 'numeric', weekday: 'short',
}).format(new Date(`${date}T12:00:00`));

export const timeLabel = (date: string) => new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Shanghai',
}).format(new Date(date));
