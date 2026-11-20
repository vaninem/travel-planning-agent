import { useEffect, useMemo, useState } from 'react';
import {
  ArrowDown, ArrowUp, BookOpen, Check, ChevronDown, Clock3, ExternalLink, Hotel,
  CloudSun, ListOrdered, MapPin, Pencil, Route, X,
} from 'lucide-react';
import { dateLabel, getForecast } from './api';
import type { Day, Forecast, Itinerary, Mode, Place, Stop } from './api';
import { BudgetPanel, HotelPrice, lodgingSearchLinks, PlaceLinks, TicketPrice, TransitFares, TravelLinks } from './Pricing';
import { TripMap } from './TripMap';

const noStops: Stop[] = [];
const noPath: [number, number][] = [];

function coordinates(value: unknown): [number, number][] {
  if (typeof value === 'string') return value.split(';').flatMap(piece => coordinates([piece.split(',').map(Number)]));
  if (!Array.isArray(value)) return [];
  return value.filter(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite)) as [number, number][];
}

function routePath(day?: Day): [number, number][] {
  if (!day?.legs?.length) return noPath;
  const paths = day.legs.map(leg => coordinates(leg.path ?? leg.polyline ?? leg.coordinates));
  return paths.every(path => path.length > 1) ? paths.flat() : noPath;
}

function weatherLabel(day?: Day) {
  const forecast = day?.weather;
  if (!forecast) return '';
  const description = forecast.summary ?? forecast.day_weather ?? forecast.dayweather ?? forecast.weather ?? forecast.description ?? forecast.condition;
  const high = forecast.high_c ?? forecast.high ?? forecast.daytemp;
  const low = forecast.low_c ?? forecast.low ?? forecast.nighttemp;
  return [
    typeof description === 'string' ? description : '',
    low != null && high != null ? `${low}–${high} ℃` : high != null ? `最高 ${high} ℃` : low != null ? `最低 ${low} ℃` : '',
  ].filter(Boolean).join(' · ');
}

function legLabel(leg: Record<string, unknown> | undefined) {
  if (!leg) return '';
  const seconds = Number(leg.duration_seconds ?? leg.duration_s);
  const minutes = Number(leg.duration_minutes ?? leg.minutes ?? leg.travel_minutes ?? (Number.isFinite(seconds) ? seconds / 60 : undefined));
  return Number.isFinite(minutes) && minutes >= 0 ? `通勤约 ${Math.max(1, Math.round(minutes))} 分钟` : '';
}

export function ItineraryView({itinerary, jobId, requestedBudget, mode, onSave}: {
  itinerary: Itinerary;
  jobId?: string;
  requestedBudget?: number | null;
  mode?: Mode;
  onSave?: (dayIndex: number, ids: string[]) => Promise<void>;
}) {
  const [dayIndex, setDayIndex] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [ordered, setOrdered] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [forecasts, setForecasts] = useState<Forecast[]>([]);
  useEffect(() => {
    if (!jobId || mode !== 'live') return;
    const controller = new AbortController();
    getForecast(jobId, controller.signal).then(result => setForecasts(result.forecasts)).catch(() => undefined);
    return () => controller.abort();
  }, [jobId, mode]);
  const forecastsByDate = new Map(forecasts.map(forecast => [forecast.date, forecast]));
  const forecastDays = itinerary.days.map(day => {
    const refreshed = forecastsByDate.get(day.date);
    return refreshed ? { ...day, weather: {...refreshed} } : day;
  });
  const showForecast = forecastDays.some(day => day.weather);
  const hotel = itinerary.hotel;
  const lodgingLinks = lodgingSearchLinks(itinerary);
  const day = itinerary.days[dayIndex] ?? itinerary.days[0];
  const sourceStops = day?.stops ?? noStops;
  const allStops = useMemo(() => itinerary.days.flatMap(item => item.stops), [itinerary.days]);
  const otherDayIds = useMemo(() => new Set(allStops.filter(item => !sourceStops.includes(item)).map(item => item.poi_id)), [allStops, sourceStops]);
  const places = useMemo(() => {
    const entries = new Map<string, Place>();
    for (const place of [...(itinerary.candidates ?? []), ...allStops]) entries.set(place.poi_id, place);
    return entries;
  }, [itinerary.candidates, allStops]);
  const stops = useMemo(() => ordered ? ordered.flatMap((id, index) => {
    const place = places.get(id);
    return place ? [{
      ...place, start_time: sourceStops[index].start_time, end_time: sourceStops[index].end_time,
      reason: sourceStops.find(stop => stop.poi_id === id)?.reason,
    }] : [];
  }) : sourceStops, [ordered, places, sourceStops]);
  const path = useMemo(() => ordered ? noPath : routePath(day), [day, ordered]);
  const active = selected ?? stops[0]?.poi_id ?? null;
  const weather = weatherLabel(forecastDays[dayIndex] ?? forecastDays[0]);

  function changeDay(index: number) {
    setDayIndex(index);
    setSelected(null);
    setOrdered(null);
    setEditError(null);
  }

  function move(index: number, offset: number) {
    if (!ordered) return;
    const copy = [...ordered];
    const next = index + offset;
    [copy[index], copy[next]] = [copy[next], copy[index]];
    setOrdered(copy);
  }

  async function save() {
    if (!onSave || !ordered) return;
    setSaving(true);
    setEditError(null);
    try {
      await onSave(dayIndex, ordered);
      setOrdered(null);
    } catch (error) {
      setEditError(error instanceof Error ? error.message : '行程保存失败。');
    } finally {
      setSaving(false);
    }
  }

  return <div className="itinerary-result">
    <section className="panel result-panel" aria-labelledby="trip-itinerary">
      <header className="result-heading">
        <div>
          <div className="result-kicker">{`${itinerary.city} · ${itinerary.days.length} 天`}</div>
          <h2 id="trip-itinerary">{itinerary.city}行程</h2>
        </div>
        {onSave && !ordered && <button className="secondary-button" type="button" onClick={() => {
            setOrdered(sourceStops.map(stop => stop.poi_id));
            setEditError(null);
          }} disabled={!sourceStops.length}><Pencil size={14}/> 调整景点</button>}
      </header>
      <BudgetPanel summary={itinerary.budget_summary} requestedBudget={requestedBudget} mode={mode ?? itinerary.mode}/>
      <div className="day-tabs" aria-label="行程日期">
        {itinerary.days.map((item, index) => <button key={item.date} type="button" className={dayIndex === index ? 'day-tab day-selected' : 'day-tab'}
          onClick={() => changeDay(index)} aria-pressed={dayIndex === index} disabled={saving}>
          <strong>第 {index + 1} 天</strong><span>{dateLabel(item.date)}</span>
        </button>)}
      </div>
      <div className="day-workspace">
        <div className="stops-column">
          <div className="stops-heading"><ListOrdered size={16}/><span>当天安排</span>
            <span className="stops-meta">{weather || `${stops.length} 个地点`}</span>
          </div>
          {stops.length ? <ol className="stop-list">
            {stops.map((stop, index) => {
              const travel = ordered ? '' : legLabel(day?.legs?.[index]);
              const group = stop.pricing?.admission_group;
              const previousPrice = stops[index - 1]?.pricing;
              const sharedTicket = !!group && previousPrice?.status === 'quoted' && group === previousPrice.admission_group;
              const options = ordered && [...places.values()].filter(place => place.poi_id === stop.poi_id
                || (!ordered.includes(place.poi_id) && !otherDayIds.has(place.poi_id)));
              return <li className="stop-list-item" key={index}>
                <button type="button" className={stop.poi_id === active ? 'stop-button stop-selected' : 'stop-button'}
                  onClick={() => setSelected(stop.poi_id)} aria-pressed={stop.poi_id === active}>
                  <span className="stop-number">{index + 1}</span>
                  <span className="stop-content">
                    <span className="stop-time"><Clock3 size={11}/>{stop.start_time}–{stop.end_time}</span>
                    <strong>{stop.name}</strong>
                    {stop.address && <span className="stop-address">{stop.address}</span>}
                    {stop.reason && <span className="stop-reason">{stop.reason}</span>}
                    <TicketPrice pricing={stop.pricing} budgeted={Boolean(requestedBudget ?? itinerary.budget_summary?.limit)} shared={sharedTicket}/>
                  </span>
                </button>
                <PlaceLinks place={stop} city={itinerary.city}/>
                {stop.pricing?.status !== 'sample' && !!stop.pricing?.notes?.length &&
                  <ul className="price-notes">{stop.pricing.notes.map(note => <li key={note}>{note}</li>)}</ul>}
                {ordered && <div className="stop-controls">
                  <button type="button" className="icon-button" title="上移" aria-label={`上移 ${stop.name}`}
                    disabled={index === 0 || saving} onClick={() => move(index, -1)}><ArrowUp size={14}/></button>
                  <button type="button" className="icon-button" title="下移" aria-label={`下移 ${stop.name}`}
                    disabled={index === stops.length - 1 || saving} onClick={() => move(index, 1)}><ArrowDown size={14}/></button>
                  {options && options.length > 1 && <div className="field-wrap replacement">
                    <select aria-label={`替换 ${stop.name}`} value={stop.poi_id} disabled={saving} onChange={event => {
                      const next = ordered.map((item, i) => i === index ? event.target.value : item);
                      setOrdered(next);
                      setSelected(event.target.value);
                    }}>
                      {options.map(place => <option key={place.poi_id} value={place.poi_id}>
                        {place.poi_id === stop.poi_id ? '替换景点…' : place.name}
                      </option>)}
                    </select><ChevronDown className="select-chevron" size={13}/>
                  </div>}
                </div>}
                {travel && index < stops.length - 1 && <div className="stop-travel"><Route size={12}/>{travel}</div>}
              </li>;
            })}
          </ol> : <p className="empty-day">当天没有可展示的地点。</p>}
          {ordered && <div className="edit-actions">
            {editError && <p className="form-error" role="alert">{editError}</p>}
            <button className="secondary-button" type="button" disabled={saving} onClick={() => {setOrdered(null); setEditError(null); setSelected(null);}}>
              <X size={14}/> 取消
            </button>
            <button className="primary-button mini" type="button" disabled={saving} onClick={() => void save()}>
              <Check size={15}/> {saving ? '保存中…' : '保存行程'}
            </button>
          </div>}
        </div>
        <div className="map-column">
          <div className="stops-heading"><MapPin size={16}/><span>行程地图</span></div>
          <TripMap stops={stops} path={path} hotel={hotel} activeId={active}
            onSelect={setSelected} city={itinerary.city} example={(mode ?? itinerary.mode) === 'fixture'}/>
        </div>
      </div>
    </section>
    {showForecast && <section className="panel trip-weather" aria-labelledby="trip-forecast-heading">
      <div className="panel-heading"><span className="section-icon"><CloudSun size={18}/></span><h3 id="trip-forecast-heading">逐日天气预报</h3></div>
      <div className="forecast-days">{forecastDays.map((item, index) => {
        const forecast = item.weather;
        const label = weatherLabel(item);
        const source = forecast?.source_url;
        const chance = forecast?.rain_chance;
        return <article className="forecast-day" key={item.date}>
          <h4>{dateLabel(item.date)} <span>第 {index + 1} 天</span></h4>
          <p>{label || '暂无这一天的预报'}</p>
          {typeof chance === 'number' && <small>当天最高降水概率 {chance}%</small>}
          {typeof source === 'string' && source.startsWith('https://') ? <a href={source} target="_blank" rel="noopener noreferrer">
            {typeof forecast?.source_name === 'string' ? forecast.source_name : '天气来源'}
          </a> : forecast?.evidence === 'amap' || forecast?.evidence === 'amap_mcp' ? <small>高德天气</small> : null}
        </article>;
      })}</div>
    </section>}
    {(hotel || lodgingLinks.length > 0 || Boolean(itinerary.guides?.length) || Boolean(itinerary.travel_links?.length) || Boolean(itinerary.transit_fare_reference?.length)) && <section className="panel trip-details" aria-label="行程详情">
      {(hotel || lodgingLinks.length > 0) && <div className="detail-item">
        <span className="section-icon"><Hotel size={17}/></span>
        <div>
          <h3>{hotel ? '住宿候选' : '查找住宿'}</h3>
          {hotel && <>
            <button className="text-button detail-place" onClick={() => setSelected(hotel.poi_id)}>{hotel.name}</button>
            {hotel.address && <p>{hotel.address}</p>}
            <HotelPrice pricing={hotel.pricing}/><PlaceLinks place={hotel} hotel/>
          </>}
          {lodgingLinks.length > 0 && <>
            <p>{hotel ? '可按行程日期和人数查看房价与可订情况。' : '暂未核实酒店候选。可按行程日期和人数查找住宿。'}</p>
            <div className="place-links">{lodgingLinks.map(({href, label}) => <a key={href} href={href}
              target="_blank" rel="noopener noreferrer">{label}<ExternalLink size={12} aria-hidden="true"/></a>)}</div>
          </>}
        </div>
      </div>}
      {!!itinerary.transit_fare_reference?.length && <TransitFares fares={itinerary.transit_fare_reference}/>}
      {!!itinerary.travel_links?.length && <TravelLinks links={itinerary.travel_links}/>}
      {!!itinerary.guides?.length && <div className="detail-item">
        <span className="section-icon"><BookOpen size={17}/></span>
        <div><h3>参考攻略</h3>
          {itinerary.guides.map((guide, index) => {
            const title = Array.isArray(guide.heading) ? guide.heading.join(' › ') : guide.heading;
            return <details className="guide-result" key={`${guide.source}-${index}`}>
              <summary>{title || guide.source}</summary>
              <p>{guide.content}</p><small>{guide.source}{guide.start_line ? ` · 第 ${guide.start_line}${guide.end_line && guide.end_line !== guide.start_line ? `–${guide.end_line}` : ''} 行` : ''}</small>
            </details>;
          })}
        </div>
      </div>}
    </section>}
  </div>;
}
