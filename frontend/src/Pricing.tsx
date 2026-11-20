import { ChevronDown, ExternalLink, Wallet } from 'lucide-react';
import type { BudgetSummary, Itinerary, Mode, Place, PlacePricing } from './api';

function currencyAmount(value: number, currency = 'CNY') {
  const formatter = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: /^[A-Z]{3}$/.test(currency) ? currency : 'CNY',
    minimumFractionDigits: 0, maximumFractionDigits: 2 });
  return formatter.format(value);
}

function publishedAmount(amount: number, currency?: string, kind?: PlacePricing['price_kind'], maximum?: number | null) {
  const minimum = currencyAmount(amount, currency);
  if (kind === 'range' && maximum != null) return `${minimum}–${currencyAmount(maximum, currency)}`;
  return kind === 'from' ? `${minimum} 起` : minimum;
}

function safeSource(value?: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

const budgetStatuses = {within: '预算内', over: '超出预算', partial: '部分费用待核实'} as const;

export function BudgetPanel({summary, requestedBudget, mode}: {
  summary?: BudgetSummary | null;
  requestedBudget?: number | null;
  mode?: Mode;
}) {
  if (!summary) {
    if (requestedBudget == null || requestedBudget <= 0) return null;
    return <section className="budget-panel" aria-label="费用预算">
      <div className="budget-title"><Wallet size={16}/><h3>费用预算</h3></div>
      <p>预算上限 <strong>{currencyAmount(requestedBudget)}</strong>。这份历史行程暂无费用估算。</p>
    </section>;
  }

  const amount = (value: number) => currencyAmount(value, summary.currency);
  const partial = summary.estimated_total === null;
  let difference = `按已估费用剩 ${amount(Math.max(0, summary.limit - summary.estimated_subtotal))}`;
  if (summary.remaining !== null) {
    if (summary.remaining < 0) difference = `${partial ? '至少超出' : '超出'} ${amount(-summary.remaining)}`;
    else if (!partial) difference = summary.remaining === 0 ? '与预算持平' : `预计剩余 ${amount(summary.remaining)}`;
  }
  const status = partial && summary.status === 'over' ? '已知费用超出预算' : budgetStatuses[summary.status];

  return <section className="budget-panel" aria-label="费用预算">
    <header className="budget-heading">
      <div className="budget-title"><Wallet size={16}/><h3>费用预算</h3><span>{mode === 'fixture' ? '样本估算' : '按可查费用估算'}</span></div>
      <span className={`budget-status budget-status-${summary.status}`}>{status}</span>
    </header>
    <dl className="budget-totals">
      <div><dt>预算上限</dt><dd>{amount(summary.limit)}</dd></div>
      <div><dt>{partial ? '已估小计' : '预计总支出'}</dt><dd>{amount(summary.estimated_total ?? summary.estimated_subtotal)}</dd></div>
      <div><dt>与预算对比</dt><dd className={`budget-difference budget-difference-${summary.status}`}>{difference}</dd></div>
    </dl>
    {!!summary.provisional_tickets && <p className="budget-help">{summary.provisional_tickets} 项门票待核实，预算暂按 ¥0 计。</p>}
    {partial && <p className="budget-help">小计仅包含已知费用，实际总支出仍待确认。</p>}
    {(summary.items.length > 0 || summary.notes.length > 0) && <details className="budget-breakdown">
      <summary><ChevronDown size={14}/>费用明细</summary>
      <dl className="budget-breakdown-list">{summary.items.map(item => <div key={item.key}>
        <dt>{item.label}</dt><dd>{item.amount === null
          ? item.known_amount ? `已知 ${amount(item.known_amount)}，另有费用待核实` : '待核实'
          : amount(item.amount)}</dd>
      </div>)}</dl>
      {!!summary.foreign_costs?.length && <div className="foreign-costs"><h4>全团外币票价</h4>
        <dl className="budget-breakdown-list">{summary.foreign_costs.map((item, index) => <div key={`${item.label}-${index}`}>
          <dt>{item.label}</dt><dd>{publishedAmount(item.amount, item.currency, item.price_kind, item.maximum)}</dd>
        </div>)}</dl>
      </div>}
      {summary.notes.length > 0 && <ul className="price-notes">{summary.notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
    </details>}
  </section>;
}

export function PlaceLinks({place, hotel = false, city}: {place: Place; hotel?: boolean; city?: string}) {
  const placeUrl = safeSource(place.source_url);
  const unverified = !hotel && (!place.pricing || place.pricing.status === 'unknown');
  const priceUrl = safeSource(place.pricing?.source_url) || (unverified ? safeSource(place.official_website) : null);
  const ticketLookup = unverified && !priceUrl && city
    ? `https://www.bing.com/search?${new URLSearchParams({q: `${city} ${place.name} 门票 官网`})}`
    : null;
  if (!placeUrl && !priceUrl && !ticketLookup) return null;
  const priceLabel = hotel && place.pricing?.status === 'unknown' ? '搜索该城市住宿' : hotel ? '查看房价与预订'
    : unverified ? '核实门票' : '票价来源';
  const placeLabel = hotel ? (place.source_name?.includes('住宿搜索') ? '搜索该城市住宿' : '酒店链接') : '景点链接';
  return <div className="place-links">
    {placeUrl && placeUrl !== priceUrl && <a href={placeUrl} target="_blank" rel="noopener noreferrer"
      title={place.source_name || undefined}>{placeLabel}<ExternalLink size={12} aria-hidden="true"/></a>}
    {priceUrl && <a href={priceUrl} target="_blank" rel="noopener noreferrer"
      title={place.pricing?.source_name || place.source_name || undefined}>{priceLabel}<ExternalLink size={12} aria-hidden="true"/></a>}
    {ticketLookup && <a href={ticketLookup} target="_blank" rel="noopener noreferrer">查找官方票务<ExternalLink size={12} aria-hidden="true"/></a>}
  </div>;
}

export function lodgingSearchLinks(itinerary: Itinerary) {
  const { hotel, lodging_search: search } = itinerary;
  if (!search) return [];
  const quoteUrl = safeSource(hotel?.pricing?.source_url);
  if (hotel?.pricing?.status === 'quoted' && quoteUrl) return [];
  const seen = new Set([quoteUrl, safeSource(hotel?.source_url),
    ...(itinerary.travel_links ?? []).map(link => safeSource(link.source_url))]);
  return [
    {href: safeSource(search.source_url), label: search.source_name || '按日期查找住宿'},
    {href: safeSource(search.alternate_url), label: search.alternate_name || '查询另一家住宿平台'},
  ].flatMap(({href, label}) => {
    if (!href || seen.has(href)) return [];
    seen.add(href);
    return [{href, label}];
  });
}

export function TravelLinks({links}: {links: NonNullable<Itinerary['travel_links']>}) {
  const safe = links.flatMap(link => {
    const href = safeSource(link.source_url);
    return href ? [{...link, href}] : [];
  });
  if (!safe.length) return null;
  return <div className="detail-item">
    <ExternalLink size={18} aria-hidden="true"/>
    <div><h3>携程查询结果</h3>
      <div className="place-links">{safe.map(link => <a href={link.href} key={link.href} target="_blank"
        rel="noopener noreferrer" title={link.source_name}>{link.label}<ExternalLink size={12} aria-hidden="true"/></a>)}</div>
    </div>
  </div>;
}

export function TicketPrice({pricing, budgeted, shared}: {pricing?: PlacePricing | null; budgeted?: boolean; shared?: boolean}) {
  if (!pricing || pricing.status === 'unknown') return <span className="ticket-price price-unknown">门票待核实{budgeted && ' · 暂按 ¥0'}</span>;
  if (pricing.status === 'free') return <span className="ticket-price">门票免费</span>;
  if (pricing.amount == null) return <span className="ticket-price price-unknown">门票待核实</span>;
  if (shared) return <span className="ticket-price">与上一站共用门票</span>;
  return <span className="ticket-price">{pricing.price_kind === 'reference' ? '参考票价' : '成人门票'} {publishedAmount(pricing.amount, pricing.currency, pricing.price_kind, pricing.maximum)} / 人
    {pricing.status === 'sample' && <span className="sample-price">样本估算</span>}
  </span>;
}

export function TransitFares({fares}: {fares: NonNullable<Itinerary['transit_fare_reference']>}) {
  return <div className="detail-item">
    <span className="section-icon"><ExternalLink size={16}/></span>
    <div><h3>公共交通票价参考</h3><ul>{fares.map(fare => {
      const url = safeSource(fare.source_url);
      return <li key={fare.label}>{fare.label} {currencyAmount(fare.amount, fare.currency)}
        {url && <> · <a href={url} target="_blank" rel="noopener noreferrer">官方票价</a></>}</li>;
    })}</ul><p>以所乘线路、换乘和折扣规则为准。</p></div>
  </div>;
}

export function HotelPrice({pricing}: {pricing?: PlacePricing | null}) {
  const stayLabel = pricing?.nights != null ? `${pricing.nights} 晚合计` : '住宿合计';
  if (!pricing || pricing.status === 'unknown' || (pricing.nightly == null && pricing.total == null && !pricing.per_night?.length)) {
    return <p className="hotel-price price-unknown">每晚房价与住宿合计待核实</p>;
  }
  return <div className="hotel-price">
    <p className="hotel-costs">
      <span>{pricing.nightly != null ? <>{(pricing.nights ?? 0) > 1 && '平均每晚 '}<strong>{currencyAmount(pricing.nightly, pricing.currency)}</strong>
        {(pricing.nights ?? 0) <= 1 && ' / 晚'}</>
        : pricing.per_night?.length ? '房价按日期浮动' : '每晚房价待核实'}</span>
      <span>{pricing.total_estimated && '预计 '}{stayLabel} {pricing.total != null ? <strong>{currencyAmount(pricing.total, pricing.currency)}</strong> : '待核实'}</span>
      {pricing.rooms != null && <span>{pricing.rooms} 间房</span>}
      {pricing.status === 'sample' && <span className="sample-price">样本估算</span>}
    </p>
    {!!pricing.per_night?.length && <details className="nightly-price" open>
      <summary>逐晚房价</summary>
      <ul>{pricing.per_night.map(item => <li key={item.date}>{item.date} <strong>{currencyAmount(item.amount, pricing.currency)}</strong></li>)}</ul>
    </details>}
    {!!pricing.notes?.length && <ul className="price-notes">{pricing.notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
  </div>;
}
