import { useEffect, useState } from 'react';
import { Activity, AlertCircle, Check, ChevronDown, Circle, History, Pause, Play, RotateCcw, WifiOff } from 'lucide-react';
import { isTerminal, stageLabels, statusLabels, timeLabel } from './api';
import type { Job, JobEvent, Stage } from './api';

export type Connection = 'idle' | 'connected' | 'reconnecting';
const stages: Stage[] = ['research', 'draft', 'validate'];
const eventLabels: Record<string, string> = {
  queued: '任务已提交', claimed: '开始执行', stage_completed: '阶段完成', retry_scheduled: '等待重试',
  succeeded: '行程已就绪', dead: '规划失败', itinerary_updated: '保存行程修改',
};

export function Progress({job, events, connection, onRefresh}: {
  job: Job;
  events: JobEvent[];
  connection: Connection;
  onRefresh: () => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [cursor, setCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const eventCount = events.length;
  const position = cursor ?? eventCount;
  const current = stages.indexOf(job.stage);
  const failed = job.status === 'dead';
  const done = job.status === 'succeeded';
  const relevantEvents = events.slice(0, position);
  useEffect(() => {
    if (!playing) return;
    if (position >= eventCount) {
      setPlaying(false);
      return;
    }
    const timer = window.setTimeout(() => setCursor(position + 1), 500);
    return () => window.clearTimeout(timer);
  }, [position, playing, eventCount]);
  const togglePlayback = () => {
    if (!playing && position >= eventCount) setCursor(0);
    setPlaying(value => !value);
  };

  return <section className="panel progress-panel" aria-labelledby="job-progress">
    <header className="progress-header">
      <div className="panel-heading"><span className="section-icon"><Activity size={17}/></span><h2 id="job-progress">任务进度</h2></div>
      <span className={`status-chip status-${job.status}`} role="status">{statusLabels[job.status]}</span>
    </header>
    <ol className="stages">
      {stages.map((stage, index) => {
        const complete = done || (current !== -1 && index < current);
        const active = index === current && !isTerminal(job.status);
        return <li key={stage} className={complete ? 'is-complete' : active ? 'is-active' : ''} aria-current={active ? 'step' : undefined}>
          <span>{complete ? <Check size={13}/> : active ? <span className="stage-pulse"/> : <Circle size={8}/>}</span>
          {stageLabels[stage]}
        </li>;
      })}
    </ol>
    {connection === 'reconnecting' && <div className="connection-message" role="status">
      <WifiOff size={14}/> 进度连接正在恢复 <button type="button" className="text-button" onClick={onRefresh}>刷新状态</button>
    </div>}
    {job.status === 'retrying' && <p className="connection-message" role="status"><RotateCcw size={14}/>尝试次数 {job.attempts} / {job.max_attempts}</p>}
    {failed && job.error && <p className="job-error" role="alert"><AlertCircle size={16}/>{job.error}</p>}
    <button type="button" className="progress-toggle text-button" aria-expanded={detailsOpen} onClick={() => setDetailsOpen(value => !value)}>
      <History size={15}/> 执行记录 <span>{eventCount}</span><ChevronDown size={14} className={detailsOpen ? 'flipped' : ''}/>
    </button>
    {detailsOpen && <div className="event-playback">
      <div className="playback-toolbar">
        <button type="button" className="secondary-button mini" onClick={togglePlayback} disabled={!eventCount}>
          {playing ? <Pause size={13}/> : <Play size={13}/>} {playing ? '暂停' : '回放'}
        </button>
        <input type="range" aria-label="回放进度" min={0} max={eventCount} value={position} disabled={!eventCount}
          onChange={event => {setPlaying(false); setCursor(Number(event.target.value));}}/>
        <span>{position} / {eventCount}</span>
        {cursor !== null && <button className="text-button" onClick={() => {setPlaying(false); setCursor(null);}}>最新</button>}
      </div>
      <ol className="event-list" aria-label="任务事件">
        {relevantEvents.map(item => <li key={item.id}>
          <time dateTime={item.created_at}>{timeLabel(item.created_at)}</time>
          <div><strong>{eventLabels[item.type] ?? stageLabels[item.stage]}</strong>
            {item.message && item.message !== eventLabels[item.type] && <p>{item.message}</p>}</div>
        </li>)}
      </ol>
      {!relevantEvents.length && <p className="field-help">回放进度起点</p>}
    </div>}
  </section>;
}
