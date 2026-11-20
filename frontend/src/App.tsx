import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowDownRight, ArrowRight, CalendarDays, Clock3, Compass, History, Map as MapIcon, MapPin, Plus, RotateCcw, Route,
} from 'lucide-react';
import {
  ApiError, createJob, dateLabel, getConfig, getEvents, getJob, getJobs, isTerminal, statusLabels,
  retryJob, transportLabels, updateDay,
} from './api';
import type { ConfigStatus, Job, JobEvent, Strategy, TripRequest } from './api';
import { PlannerForm } from './PlannerForm';
import { Progress } from './Progress';
import type { Connection } from './Progress';
import { ItineraryView } from './ItineraryView';
const configLabels: Record<ConfigStatus, string> = {
  checking: '正在检查服务', ready: '实时服务已配置', missing: '实时服务待配置', unreachable: '规划服务未连接',
};

function mergeEvents(previous: JobEvent[], incoming: JobEvent[]) {
  const items = new Map<number, JobEvent>(previous.map(item => [item.id, item]));
  incoming.forEach(item => items.set(item.id, item));
  return Array.from(items.values()).sort((a, b) => a.id - b.id);
}

function storedJobId(): string | null {
  try {return localStorage.getItem('travel:selected-job');} catch {return null;}
}

function saveJobId(id: string) {
  try {localStorage.setItem('travel:selected-job', id);} catch {/* The directory still works without local storage. */}
}

export function App() {
  const [view, setView] = useState<'plan' | 'history'>('plan');
  const [jobId, setJobId] = useState<string | null>(storedJobId);
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [selectedError, setSelectedError] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>('idle');
  const [subscription, setSubscription] = useState(0);
  const [retryingJob, setRetryingJob] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [configStatus, setConfigStatus] = useState<ConfigStatus>('checking');
  const [hotelPricingAvailable, setHotelPricingAvailable] = useState<boolean>();
  const [defaultMealsPerPersonDay, setDefaultMealsPerPersonDay] = useState<number>();
  const previousSubmission = useRef<{signature: string; key: string} | null>(null);
  const inFlight = useRef(false);
  const loadingRef = useRef<HTMLDivElement>(null);
  const selectedJob = job?.id === jobId ? job : null;

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError(null);
    try {setJobs((await getJobs()).jobs);}
    catch (error) {setHistoryError(error instanceof Error ? error.message : '加载行程失败。');}
    finally {setHistoryLoading(false);}
  }, []);

  const loadConfig = useCallback(async () => {
    setConfigStatus('checking');
    try {
      const config = await getConfig();
      setConfigStatus(config.live_available ? 'ready' : 'missing');
      setHotelPricingAvailable(config.hotel_pricing_available ?? config.booking_available);
      setDefaultMealsPerPersonDay(config.default_meals_per_person_day);
    } catch {
      setConfigStatus('unreachable');
      setHotelPricingAvailable(undefined);
    }
  }, []);

  useEffect(() => {
    void loadHistory();
    void loadConfig();
  }, [loadHistory, loadConfig]);

  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let stream: EventSource | null = null;
    let seenId = 0;
    let eventReceived = false;
    let hasTerminated = false;
    let refreshNumber = 0;
    let lastAppliedRefresh = 0;
    let poll: number | undefined;
    let connectTimer: number | undefined;

    const end = () => {
      stream?.close();
      stream = null;
      window.clearInterval(poll);
      window.clearTimeout(connectTimer);
      poll = undefined;
      setConnection('idle');
    };
    const refresh = async () => {
      const number = ++refreshNumber;
      try {
        const current = await getJob(jobId);
        if (stopped || number < lastAppliedRefresh) return null;
        lastAppliedRefresh = number;
        setSelectedError(null);
        setJob(previous => previous?.id === current.id && previous.revision > current.revision ? previous : current);
        if (!hasTerminated && isTerminal(current.status)) {
          hasTerminated = true;
          end();
          getEvents(jobId).then(data => {
            if (!stopped) setEvents(existing => mergeEvents(existing, data.events));
          }).catch(() => undefined);
          void loadHistory();
        }
        return current;
      } catch (error) {
        if (!stopped && number >= lastAppliedRefresh) setSelectedError(error instanceof Error ? error.message : '加载任务失败。');
        return null;
      }
    };
    const start = async () => {
      const seed = await getEvents(jobId).catch(() => ({events: []}));
      if (stopped) return;
      seenId = seed.events.at(-1)?.id ?? 0;
      setEvents(seed.events);
      const current = await refresh();
      if (stopped || isTerminal(current?.status)) return;
      if ('EventSource' in window) {
        stream = new EventSource(`/api/jobs/${jobId}/stream?after=${seenId}`);
        connectTimer = window.setTimeout(() => {
          if (!eventReceived && stream?.readyState !== EventSource.OPEN) setConnection('reconnecting');
        }, 1800);
        stream.onopen = () => {if (!stopped) setConnection('connected');};
        stream.onmessage = message => {
          if (stopped) return;
          try {
            const item = JSON.parse(message.data) as JobEvent;
            eventReceived = true;
            setEvents(existing => mergeEvents(existing, [item]));
            void refresh();
          } catch {
            setConnection('reconnecting');
          }
        };
        stream.onerror = () => {
          if (stopped || !stream) return;
          setConnection('reconnecting');
          void refresh();
        };
      } else {
        setConnection('reconnecting');
      }
      poll = window.setInterval(() => {
        if (!stream || stream.readyState !== EventSource.OPEN || !eventReceived) void refresh();
      }, 5000);
    };
    setEvents([]);
    setRetryError(null);
    setSelectedError(null);
    setConnection('idle');
    void start();
    return () => {
      stopped = true;
      stream?.close();
      window.clearInterval(poll);
      window.clearTimeout(connectTimer);
    };
  }, [jobId, loadHistory, subscription]);

  async function submit(request: TripRequest, strategy: Strategy) {
    if (inFlight.current || configStatus !== 'ready') return;
    inFlight.current = true;
    setSubmitting(true);
    setFormError(null);
    const signature = JSON.stringify({request, strategy});
    const previous = previousSubmission.current;
    const key = previous?.signature === signature ? previous.key : (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`);
    previousSubmission.current = {signature, key};
    try {
      const created = await createJob(request, 'live', strategy, key);
      previousSubmission.current = null;
      setEvents([]);
      saveJobId(created.id);
      setJob(created);
      setJobId(created.id);
      setView('plan');
      void loadHistory();
      if (window.matchMedia('(max-width: 790px)').matches) {
        window.setTimeout(() => loadingRef.current?.scrollIntoView({behavior: 'smooth', block: 'start'}), 100);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status > 0 && error.status < 500) previousSubmission.current = null;
      setFormError(error instanceof Error ? error.message : '提交任务失败。');
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  function selectJob(next: Job) {
    if (jobId !== next.id) {
      setJob(next);
      setEvents([]);
      setJobId(next.id);
      saveJobId(next.id);
    }
    setView('plan');
  }

  async function saveDay(dayIndex: number, ids: string[]) {
    if (!selectedJob) return;
    try {
      const result = await updateDay(selectedJob.id, selectedJob.revision, dayIndex, ids);
      setJob(result);
      try {
        const history = await getEvents(result.id);
        setEvents(current => mergeEvents(current, history.events));
      } catch { /* The itinerary was saved. */ }
      void loadHistory();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const fresh = await getJob(selectedJob.id).catch(() => null);
        if (fresh) setJob(fresh);
        throw new Error('这份行程已有新版本。已加载最新结果，请取消编辑后重新调整。');
      }
      throw error;
    }
  }

  function refreshSelected() {
    if (!jobId) return;
    getJob(jobId).then(result => {
      setJob(result);
      setSelectedError(null);
    }).catch(error => setSelectedError(error instanceof Error ? error.message : '加载任务失败。'));
    getEvents(jobId).then(result => setEvents(current => mergeEvents(current, result.events))).catch(() => undefined);
  }

  async function retrySelected() {
    if (!selectedJob || selectedJob.mode !== 'live' || retryingJob) return;
    setRetryingJob(true);
    setRetryError(null);
    try {
      const next = await retryJob(selectedJob.id);
      setJob(next);
      setSubscription(value => value + 1);
      void loadHistory();
    } catch (error) {
      setRetryError(error instanceof Error ? error.message : '任务重试失败。');
    } finally {
      setRetryingJob(false);
    }
  }

  function clearSelection() {
    setJobId(null);
    setJob(null);
    setEvents([]);
    setSelectedError(null);
    setConnection('idle');
    try {localStorage.removeItem('travel:selected-job');} catch {/* Optional browser storage. */}
  }

  return <>
    <header className="topbar">
      <div className="site-width topbar-inner">
        <button type="button" className="brand" onClick={() => setView('plan')} aria-label="旅序，返回规划">
          <span className="brand-icon"><Compass size={22} strokeWidth={1.6}/></span>
          <span>旅序</span>
        </button>
        <nav aria-label="主导航">
          <button type="button" className={view === 'plan' ? 'nav-button current-nav' : 'nav-button'} aria-current={view === 'plan' ? 'page' : undefined}
            onClick={() => setView('plan')}><Route size={16}/> 规划行程</button>
          <button type="button" className={view === 'history' ? 'nav-button current-nav' : 'nav-button'} aria-current={view === 'history' ? 'page' : undefined}
            onClick={() => {setView('history'); void loadHistory();}}><History size={16}/> 历史行程</button>
        </nav>
        <div className={`topbar-mode config-${configStatus}`}><span/>{configLabels[configStatus]}</div>
      </div>
    </header>
    <main className="site-width page">
      {view === 'plan' ? <>
        <section className="hero" aria-labelledby="hero-heading">
          <div className="hero-content">
            <div className="hero-kicker"><span/> 智能旅行规划</div>
            <h1 id="hero-heading">下一站，去哪儿？</h1>
            <p>选个城市和出发日期，把想去的地方排进每一天。</p>
          </div>
          <div className="hero-place"><MapPin size={14}/> 封面 · 上海黄浦江畔</div>
        </section>
        <div className="workspace">
          <PlannerForm submitting={submitting} configStatus={configStatus} hotelPricingAvailable={hotelPricingAvailable}
            defaultMealsPerPersonDay={defaultMealsPerPersonDay}
            error={formError} onSubmit={(request, strategy) => void submit(request, strategy)} onRefreshConfig={() => void loadConfig()}/>
          <div className="work-column" ref={loadingRef}>
            {selectedJob && <Progress key={selectedJob.id} job={selectedJob} events={events} connection={connection} onRefresh={refreshSelected}/>}
            {selectedError && <section className="panel state-panel" role="alert">
              <p>{selectedError}</p><button className="secondary-button" onClick={refreshSelected}>重新加载</button>
              <button className="text-button" onClick={clearSelection}>关闭行程</button>
            </section>}
            {selectedJob?.itinerary ? <ItineraryView key={selectedJob.id} itinerary={selectedJob.itinerary}
              jobId={selectedJob.id} requestedBudget={selectedJob.request.budget} mode={selectedJob.mode}
              onSave={selectedJob.status === 'succeeded' ? saveDay : undefined}/>
              : !jobId ? <section className="panel state-panel empty-plan">
                <div className="state-icon"><MapIcon size={24}/></div>
                <h2>行程将在这里显示</h2>
                <p>填写目的地和日期，生成每日路线、费用及住宿信息。</p>
              </section>
                : !selectedError && <section className="panel state-panel">
                  <div className="state-icon"><MapIcon size={23}/></div>
                  <h2>{selectedJob?.status === 'dead' ? '这次规划未完成' : '正在准备行程'}</h2>
                  <p>{selectedJob?.status === 'dead' ? '可以调整目的地和规划选项后重新提交。' : '每日路线和地图会在任务完成后显示。'}</p>
                  {selectedJob?.status === 'dead' && selectedJob.mode === 'live' && <button className="secondary-button" disabled={retryingJob || configStatus !== 'ready'} onClick={() => void retrySelected()}>
                    <RotateCcw size={14}/> {retryingJob ? '正在提交…' : '重试任务'}
                  </button>}
                  {retryError && <p className="form-error" role="alert">{retryError}</p>}
                </section>}
          </div>
        </div>
      </> : <section className="history-view" aria-labelledby="history-title">
        <header className="history-title">
          <div><h1 id="history-title">历史行程</h1><p>查看已提交的旅行计划和执行记录。</p></div>
          <button className="primary-button" onClick={() => setView('plan')}><Plus size={17}/> 新建行程</button>
        </header>
        {historyLoading && <p className="panel state-panel" role="status">正在加载行程…</p>}
        {historyError && <div className="panel state-panel"><p role="alert">{historyError}</p><button className="secondary-button" onClick={() => void loadHistory()}>重新加载</button></div>}
        {!historyLoading && !historyError && !jobs.length && <div className="panel state-panel">
          <div className="state-icon"><Clock3 size={23}/></div><h2>还没有旅行计划</h2>
          <button className="secondary-button" onClick={() => setView('plan')}>开始规划 <ArrowDownRight size={15}/></button>
        </div>}
        {!historyError && jobs.length > 0 && <ul className="history-list">
          {jobs.map(item => <li key={item.id}>
            <button className="panel history-card" onClick={() => selectJob(item)}>
              <span className="history-map-icon"><MapPin size={23}/></span>
              <span className="history-info"><strong>{item.request.city}</strong><span><CalendarDays size={14}/>
                {dateLabel(item.request.start_date)} · {item.request.days} 天 · {transportLabels[item.request.transport]}</span></span>
              <span className={`status-chip status-${item.status}`}>{statusLabels[item.status]}</span>
              <ArrowRight className="history-arrow" size={18}/>
            </button>
          </li>)}
        </ul>}
      </section>}
    </main>
  </>;
}
