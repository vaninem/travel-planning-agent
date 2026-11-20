import { CalendarDays, ChevronDown, Compass, Info, MapPin, Sparkles, Users, Utensils, Wallet } from 'lucide-react';
import type { FormEvent } from 'react';
import { useState } from 'react';
import { transportLabels } from './api';
import type { ConfigStatus, Strategy, Transport, TripRequest } from './api';

const interestOptions = ['经典地标', '博物馆', '美食', '自然风光', '亲子', '室内游'];
const mealAmount = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});

function chinaDate() {
  const today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts();
  const part = (key: string) => today.find(item => item.type === key)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function PlannerForm({submitting, configStatus, hotelPricingAvailable, defaultMealsPerPersonDay, error, onSubmit, onRefreshConfig}: {
  submitting: boolean;
  configStatus: ConfigStatus;
  hotelPricingAvailable?: boolean;
  defaultMealsPerPersonDay?: number;
  error: string | null;
  onSubmit: (request: TripRequest, strategy: Strategy) => void;
  onRefreshConfig: () => void;
}) {
  const [city, setCity] = useState('');
  const [date, setDate] = useState(chinaDate);
  const [days, setDays] = useState('2');
  const [travelers, setTravelers] = useState('2');
  const [interests, setInterests] = useState(['经典地标', '美食']);
  const [transport, setTransport] = useState<Transport>('transit');
  const [hotel, setHotel] = useState(true);
  const [budget, setBudget] = useState('');
  const [mealRate, setMealRate] = useState<string | null>(null);
  const [notes, setNotes] = useState('');
  const [pace, setPace] = useState<NonNullable<TripRequest['pace']>>('balanced');
  const [required, setRequired] = useState('');
  const [preferIndoor, setPreferIndoor] = useState(true);
  const [validation, setValidation] = useState<string | null>(null);
  const [strategy, setStrategy] = useState<Strategy>('verified');
  const [advanced, setAdvanced] = useState(false);
  const dailyMeals = mealRate ? Number(mealRate) : defaultMealsPerPersonDay;
  const mealsTotal = dailyMeals === undefined || !travelers || !days ? null : dailyMeals * Number(travelers) * Number(days);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (configStatus !== 'ready') return;
    const requiredPlaces = [...new Set(required.split(/[,，、\n]/).map(item => item.trim()).filter(Boolean))];
    if (!city.trim()) {
      setValidation('请输入目的地。');
      return;
    }
    if (requiredPlaces.length > 12 || requiredPlaces.some(item => item.length > 80)) {
      setValidation('必去地点最多填写 12 个，每个地点不超过 80 字。');
      return;
    }
    setValidation(null);
    onSubmit({
      city: city.trim(), start_date: date, days: Number(days), travelers: Number(travelers), interests,
      budget: budget ? Number(budget) : null, transport, hotel, notes: notes.trim(),
      ...(mealRate ? {meals_per_person_day: Number(mealRate)} : {}),
      pace, required_places: requiredPlaces, avoid_outdoors_in_rain: preferIndoor,
    }, strategy);
  }
  return <section className="panel form-panel" aria-labelledby="new-trip">
    <div className="panel-heading"><span className="section-icon"><Compass size={18}/></span><h2 id="new-trip">新建行程</h2></div>
    <form onSubmit={submit}>
      <div className="destinations">
        <label htmlFor="trip-city">目的地</label>
        <div className="field-wrap"><MapPin size={16}/>
          <input id="trip-city" className="input-icon" value={city}
            onChange={event => {setCity(event.target.value); setValidation(null);}}
            placeholder="输入城市名称" maxLength={40} autoComplete="off" required/>
        </div>
      </div>
      <div className="budget-input">
        <div className="field-pair budget-fields">
          <div>
            <div className="budget-input-heading"><label htmlFor="trip-budget">总预算（元）</label><span>选填</span></div>
            <div className="field-wrap"><Wallet size={16}/>
              <input id="trip-budget" className="input-icon" type="number" min="0.01" step="0.01" value={budget}
                onChange={event => setBudget(event.target.value)} placeholder="例如 2,000" aria-describedby="budget-help"/>
            </div>
          </div>
          <div>
            <label htmlFor="trip-meals">餐饮（元/人/天）</label>
            <div className="field-wrap"><Utensils size={16}/>
              <input id="trip-meals" className="input-icon" type="number" min="0" max="1000000" step="0.01"
                value={mealRate ?? defaultMealsPerPersonDay ?? ''} placeholder={defaultMealsPerPersonDay?.toString()}
                onChange={event => setMealRate(event.target.value)} aria-describedby="meal-help"/>
            </div>
          </div>
        </div>
        <p className="field-help" id="meal-help">{mealsTotal === null ? '按每人每天填写餐饮预留。' :
          `全团餐饮预留 ¥${mealAmount.format(mealsTotal)}（${travelers} 人 × ${days} 天）`}</p>
        <p className="field-help" id="budget-help">总预算包含市内交通、餐饮、门票和住宿，往返目的地的机票和车票另计。</p>
      </div>
      <div className="field-pair">
        <div>
          <label htmlFor="trip-date">出发日期</label>
          <div className="field-wrap"><CalendarDays size={15}/><input id="trip-date" className="input-icon date-input" type="date"
            value={date} onChange={event => setDate(event.target.value)} required/></div>
        </div>
        <div>
          <label htmlFor="trip-days">旅行天数</label>
          <input id="trip-days" type="number" min="1" max="14" value={days} onChange={event => setDays(event.target.value)} required/>
        </div>
      </div>
      <div className="field-pair">
        <div>
          <label htmlFor="trip-travelers">出行人数</label>
          <div className="field-wrap"><Users size={16}/><input id="trip-travelers" className="input-icon" type="number"
            min="1" max="30" value={travelers} onChange={event => setTravelers(event.target.value)} required/></div>
        </div>
        <div>
          <label htmlFor="trip-transport">交通方式</label>
          <div className="field-wrap">
            <select id="trip-transport" value={transport} onChange={event => setTransport(event.target.value as Transport)}>
              {Object.entries(transportLabels).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
            </select>
            <ChevronDown className="select-chevron" size={14}/>
          </div>
        </div>
      </div>
      <fieldset className="interests">
        <legend>想怎么逛</legend>
        <div className="interest-list">{interestOptions.map(interest => <button type="button" key={interest}
          className={interests.includes(interest) ? 'pill is-selected' : 'pill'} aria-pressed={interests.includes(interest)}
          onClick={() => setInterests(current => current.includes(interest) ? current.filter(item => item !== interest) : [...current, interest])}>
          {interest}</button>)}</div>
      </fieldset>
      <label className="hotel-option">
        <span>推荐住宿</span>
        <input type="checkbox" role="switch" checked={hotel} onChange={event => setHotel(event.target.checked)}/>
      </label>
      {configStatus === 'ready' && hotel && hotelPricingAvailable === false && <p className="field-help">
        实时房价尚未配置。申请密钥后，将其填入项目配置的 <code>ROLLINGGO_API_KEY</code> 并重启。{' '}
        <a href="https://travelportal-partner-center.dida.com/register?lang=zh"
          target="_blank" rel="noopener noreferrer">申请 RollingGo API Key</a>
      </p>}
      <button type="button" className="advanced-toggle" aria-expanded={advanced} aria-controls="advanced-settings"
        onClick={() => setAdvanced(value => !value)}>更多规划选项 <ChevronDown size={15} className={advanced ? 'flipped' : ''}/></button>
      {advanced && <div id="advanced-settings" className="advanced-settings">
        <div className="field-pair">
          <div>
            <label htmlFor="trip-pace">旅行节奏</label>
            <div className="field-wrap">
              <select id="trip-pace" value={pace} onChange={event => setPace(event.target.value as typeof pace)}>
                <option value="relaxed">轻松</option><option value="balanced">适中</option><option value="packed">紧凑</option>
              </select>
              <ChevronDown className="select-chevron" size={14}/>
            </div>
          </div>
          <div>
            <label htmlFor="trip-strategy">规划策略</label>
            <div className="field-wrap">
              <select id="trip-strategy" value={strategy} onChange={event => setStrategy(event.target.value as Strategy)}>
                <option value="verified">工具 + 校验</option><option value="tools">工具调用</option><option value="fixed">固定查询</option>
              </select>
              <ChevronDown className="select-chevron" size={14}/>
            </div>
          </div>
        </div>
        <div>
          <label htmlFor="trip-required">必去地点（可选）</label>
          <input id="trip-required" value={required} onChange={event => setRequired(event.target.value)}
            placeholder="例如豫园、外滩" maxLength={980}/>
        </div>
        <label className="hotel-option rain-option">
          <span>雨天优先安排室内</span>
          <input type="checkbox" role="switch" checked={preferIndoor} onChange={event => setPreferIndoor(event.target.checked)}/>
        </label>
        <div>
          <label htmlFor="trip-notes">补充要求（可选）</label>
          <textarea id="trip-notes" rows={2} maxLength={1000} value={notes} onChange={event => setNotes(event.target.value)}
            placeholder="例如希望少走路，尽量安排室内景点"/>
        </div>
      </div>}
      {(configStatus === 'missing' || configStatus === 'unreachable') && <div className="config-warning" role="status">
        <Info size={16} aria-hidden="true"/>
        <div>
          <p>{configStatus === 'missing' ? '请配置高德和模型服务后重启旅行助手。' : '暂时无法连接规划服务，请检查服务是否已启动。'}</p>
          <button type="button" className="text-button" onClick={onRefreshConfig}>重新检查服务</button>
        </div>
      </div>}
      {(validation || error) && <p className="form-error" role="alert">{validation || error}</p>}
      <button type="submit" className="primary-button submit-button" disabled={submitting || configStatus !== 'ready'}>
        <Sparkles size={16}/> {submitting ? '正在提交…' : configStatus === 'checking' ? '正在检查服务…' : '生成行程'}
      </button>
    </form>
  </section>;
}
