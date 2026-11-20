import { useEffect, useRef, useState } from 'react';
import { Compass, LocateFixed, MapPin, Maximize, Minus, Plus } from 'lucide-react';
import type { Place, Stop } from './api';
import { createStreetMap } from './streetMap';

type Coordinate = [number, number];
type Overlay = object;
interface AMapInstance {
  add: (overlays: Overlay[]) => void;
  remove: (overlays: Overlay[]) => void;
  setFitView: (overlays?: Overlay[], immediately?: boolean, padding?: number[], maxZoom?: number) => void;
  setZoomAndCenter: (zoom: number, center: Coordinate, immediately?: boolean) => void;
  on: (event: 'complete', callback: () => void) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  destroy: () => void;
}
interface AMapAPI {
  Map: new (node: HTMLElement, options: Record<string, unknown>) => AMapInstance;
  Marker: new (options: Record<string, unknown>) => Overlay;
  Polyline: new (options: Record<string, unknown>) => Overlay;
  Pixel: new (x: number, y: number) => unknown;
}
declare global {
  interface Window {
    AMap?: AMapAPI;
    _AMapSecurityConfig?: {securityJsCode?: string; serviceHost?: string};
  }
}
const mapKey = import.meta.env.VITE_AMAP_JS_KEY as string | undefined;
const mapSecurityCode = import.meta.env.VITE_AMAP_SECURITY_CODE as string | undefined;
const mapServiceHost = import.meta.env.VITE_AMAP_SERVICE_HOST as string | undefined;
let sdkPromise: Promise<AMapAPI> | undefined;

function loadAMap() {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise<AMapAPI>((resolve, reject) => {
    if (mapServiceHost) window._AMapSecurityConfig = {serviceHost: mapServiceHost};
    else if (mapSecurityCode) window._AMapSecurityConfig = {securityJsCode: mapSecurityCode};
    const script = document.createElement('script');
    const timeout = window.setTimeout(() => {
      script.remove();
      reject(new Error('高德地图加载超时'));
    }, 12_000);
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(mapKey ?? '')}`;
    script.async = true;
    script.onload = () => {
      window.clearTimeout(timeout);
      if (window.AMap) resolve(window.AMap);
      else reject(new Error('高德地图无法初始化'));
    };
    script.onerror = () => {
      window.clearTimeout(timeout);
      script.remove();
      reject(new Error('高德地图加载失败'));
    };
    document.head.appendChild(script);
  }).catch(error => {
    sdkPromise = undefined;
    throw error;
  });
  return sdkPromise;
}

interface TripMapProps {
  stops: Stop[];
  path: Coordinate[];
  hotel: Place | null;
  activeId: string | null;
  onSelect: (id: string) => void;
  city: string;
  example: boolean;
}

function mapButton(label: string, value: string, isHotel = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `map-marker${isHotel ? ' hotel-marker' : ''}`;
  button.textContent = value;
  button.setAttribute('aria-label', label);
  return button;
}

export function TripMap({stops, path, hotel, activeId, onSelect, city, example}: TripMapProps) {
  const frame = useRef<HTMLDivElement>(null);
  const target = useRef<HTMLDivElement>(null);
  const map = useRef<AMapInstance | null>(null);
  const streetNode = useRef<HTMLDivElement>(null);
  const streetMap = useRef<ReturnType<typeof createStreetMap> | null>(null);
  const overlays = useRef<Overlay[]>([]);
  const markerNodes = useRef<Map<string, HTMLButtonElement>>(new Map());
  const select = useRef(onSelect);
  const [sdk, setSdk] = useState<AMapAPI | null>(null);
  const [status, setStatus] = useState<'offline' | 'loading' | 'ready' | 'error'>(mapKey ? 'loading' : 'offline');
  const [streetStatus, setStreetStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [zoom, setZoom] = useState(1);
  const [aspect, setAspect] = useState(1);
  const [overview, setOverview] = useState(false);
  const anchor = stops[0] ?? hotel;
  const chosen = stops.find(stop => stop.poi_id === activeId) ?? (hotel?.poi_id === activeId ? hotel : null);
  const preferAMap = Boolean(mapKey && anchor && anchor.lng >= 72 && anchor.lng <= 138 && anchor.lat >= 1 && anchor.lat <= 56);
  const showAMap = preferAMap && status === 'ready';
  const useStreet = Boolean(anchor && (!preferAMap || status === 'error'));
  const showStreet = useStreet && streetStatus === 'ready';
  useEffect(() => {
    select.current = onSelect;
  }, [onSelect]);
  useEffect(() => {
    if (!frame.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      const {width, height} = entry.contentRect;
      if (width > 0 && height > 0) setAspect(width / height);
    });
    observer.observe(frame.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!preferAMap || !target.current) return;
    setStatus('loading');
    let disposed = false;
    let mapTimer: number | undefined;
    const node = target.current;
    loadAMap().then(api => {
      if (disposed) return;
      const instance = new api.Map(node, {viewMode: '2D', zoom: 12, resizeEnable: true, mapStyle: 'amap://styles/whitesmoke'});
      map.current = instance;
      mapTimer = window.setTimeout(() => {
        instance.destroy();
        map.current = null;
        setStatus('error');
      }, 10_000);
      instance.on('complete', () => {
        if (disposed || map.current !== instance) return;
        window.clearTimeout(mapTimer);
        setSdk(api);
        setStatus('ready');
      });
    }).catch(() => {
      if (!disposed) setStatus('error');
    });
    return () => {
      disposed = true;
      window.clearTimeout(mapTimer);
      map.current?.destroy();
      map.current = null;
      overlays.current = [];
    };
  }, [preferAMap]);
  useEffect(() => {
    if (!useStreet || !streetNode.current) return;
    setStreetStatus('loading');
    const instance = createStreetMap(streetNode.current, setStreetStatus);
    streetMap.current = instance;
    return () => {instance.destroy(); streetMap.current = null;};
  }, [useStreet]);
  useEffect(() => {
    streetMap.current?.plot(stops, path, hotel, example, id => select.current(id));
  }, [useStreet, stops, path, hotel, example]);
  useEffect(() => {
    streetMap.current?.highlight(activeId);
    if (activeId) streetMap.current?.focus(activeId);
  }, [useStreet, stops, path, hotel, activeId]);
  useEffect(() => {
    setOverview(false);
    setZoom(1);
  }, [stops, activeId]);
  useEffect(() => {
    const instance = map.current;
    if (!sdk || !instance) return;
    if (overlays.current.length) instance.remove(overlays.current);
    markerNodes.current.clear();
    const next: Overlay[] = stops.map((stop, index) => {
      const button = mapButton(stop.name, String(index + 1));
      button.addEventListener('click', () => select.current(stop.poi_id));
      markerNodes.current.set(stop.poi_id, button);
      return new sdk.Marker({position: [stop.lng, stop.lat], content: button, offset: new sdk.Pixel(-16, -16), title: stop.name});
    });
    if (hotel) {
      const button = mapButton(hotel.name, 'H', true);
      button.addEventListener('click', () => select.current(hotel.poi_id));
      markerNodes.current.set(hotel.poi_id, button);
      next.push(new sdk.Marker({position: [hotel.lng, hotel.lat], content: button, offset: new sdk.Pixel(-16, -16), title: hotel.name}));
    }
    const coordinates = path.length > 1 ? path : stops.map(stop => [stop.lng, stop.lat] as Coordinate);
    if (coordinates.length > 1) {
      next.push(new sdk.Polyline({
        path: coordinates, strokeColor: '#187365', strokeWeight: 5, strokeOpacity: 0.78,
        strokeStyle: path.length > 1 && !example ? 'solid' : 'dashed', lineJoin: 'round', zIndex: 20,
      }));
    }
    overlays.current = next;
    if (next.length) {
      instance.add(next);
      instance.setFitView(next, true, [55, 55, 75, 55], 15);
    }
  }, [sdk, stops, hotel, path, example]);
  useEffect(() => {
    markerNodes.current.forEach((button, id) => button.classList.toggle('active-marker', id === activeId));
    if (chosen) map.current?.setZoomAndCenter(17, [chosen.lng, chosen.lat], true);
  }, [activeId, chosen, sdk, stops, hotel, path]);

  const switchView = () => {
    setZoom(1);
    if (overview && chosen) {
      streetMap.current?.focus(chosen.poi_id);
      if (showAMap) map.current?.setZoomAndCenter(17, [chosen.lng, chosen.lat], true);
      setOverview(false);
      return;
    }
    if (showStreet) streetMap.current?.fit();
    if (showAMap && map.current && overlays.current.length) map.current.setFitView(overlays.current, false, [55, 55, 75, 55], 15);
    setOverview(true);
  };

  const points = [...stops, ...(hotel ? [hotel] : [])];
  const coordinates = [...points.map(point => [point.lng, point.lat] as Coordinate), ...path];
  const lons = coordinates.map(point => point[0]);
  const lats = coordinates.map(point => point[1]);
  const west = lons.length ? Math.min(...lons) : 0;
  const east = lons.length ? Math.max(...lons) : 0;
  const south = lats.length ? Math.min(...lats) : 0;
  const north = lats.length ? Math.max(...lats) : 0;
  const focused = !overview && chosen;
  const middleLat = focused ? focused.lat : (south + north) / 2;
  const middleLng = focused ? focused.lng : (west + east) / 2;
  const ratio = Math.cos(middleLat * Math.PI / 180) / aspect;
  const scale = focused ? 0.000075 : Math.max((east - west) * ratio / 70, (north - south) / 52, 0.00012);
  const project = ([lng, lat]: Coordinate) => [
    50 + (((lng - middleLng) * ratio) / scale) * zoom,
    50 - ((lat - middleLat) / scale) * zoom,
  ];
  const plottedPath = path.length > 1 ? path : stops.map(stop => [stop.lng, stop.lat] as Coordinate);

  return <div className="map-frame" ref={frame}>
    <div className={`amap-root ${showAMap ? '' : 'amap-hidden'}`} ref={target} aria-label={`${city}高德地图`}/>
    <div className={`street-map-root${showStreet ? '' : ' street-map-pending'}`} ref={streetNode} aria-label={`${city}街道地图`}/>
    {!showAMap && !showStreet && <div className="schematic-map" aria-label={`${city}行程坐标示意图`}>
      <div className="map-grid"/>
      <div className="map-compass"><Compass size={15}/> N</div>
      <div className="map-city">{city}</div>
      <svg className="map-route" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {plottedPath.length > 1 && <polyline points={plottedPath.map(point => project(point).join(',')).join(' ')}
          strokeDasharray={path.length > 1 && !example ? undefined : '5 5'} />}
      </svg>
      {points.map((point, index) => {
        const [left, top] = project([point.lng, point.lat]);
        return <button key={point.poi_id} className={`map-marker offline-marker${point.poi_id === activeId ? ' active-marker' : ''}${index >= stops.length ? ' hotel-marker' : ''}`}
          style={{left: `${left}%`, top: `${top}%`}} onClick={() => onSelect(point.poi_id)} title={point.name}
          aria-label={`选择 ${point.name}`} aria-pressed={point.poi_id === activeId}>{index < stops.length ? index + 1 : 'H'}</button>;
      })}
      {points.length === 0 && <div className="map-empty"><MapPin size={24}/> 当天的地点将显示在这里</div>}
    </div>}
    <div className="map-mode">{showAMap ? '高德地图' : showStreet ? '街道地图' : (preferAMap && status === 'loading') || (useStreet && streetStatus === 'loading') ? '正在加载底图' : '离线坐标示意'}{(showAMap || showStreet) && path.length < 2 && stops.length > 1 && ' · 点位连线'}</div>
    <div className="map-tools">
      <button className="icon-button" onClick={switchView} title={overview && chosen ? '查看地点周边' : '查看全程'}
        aria-label={overview && chosen ? '查看地点周边' : '查看全程'}>{overview && chosen ? <LocateFixed size={16}/> : <Maximize size={16}/>}</button>
      <button className="icon-button" onClick={() => showStreet ? streetMap.current?.zoomIn() : showAMap ? map.current?.zoomIn() : setZoom(value => Math.min(2, value + 0.2))} title="放大" aria-label="放大"><Plus size={17}/></button>
      <button className="icon-button" onClick={() => showStreet ? streetMap.current?.zoomOut() : showAMap ? map.current?.zoomOut() : setZoom(value => Math.max(0.6, value - 0.2))} title="缩小" aria-label="缩小"><Minus size={17}/></button>
    </div>
    {chosen && <div className="map-caption"><MapPin size={15}/><div><strong>{chosen.name}</strong><span>{chosen.address || `${chosen.lng.toFixed(5)}, ${chosen.lat.toFixed(5)}`}</span></div></div>}
  </div>;
}
