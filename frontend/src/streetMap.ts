import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import 'maplibre-gl/dist/maplibre-gl.css';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import './streetMap.css';
import type { Place, Stop } from './api';

type Coordinate = [number, number];

function wgsPoint([lng, lat]: Coordinate, gcj: boolean): L.LatLngTuple {
  if (!gcj || lng < 72 || lng > 137.85 || lat < 0.83 || lat > 55.83) return [lat, lng];
  const x = lng - 105, y = lat - 35, pi = Math.PI;
  const common = (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3;
  const latOffset = -100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * Math.sqrt(Math.abs(x)) + common
    + (20 * Math.sin(y * pi) + 40 * Math.sin(y / 3 * pi) + 160 * Math.sin(y / 12 * pi) + 320 * Math.sin(y * pi / 30)) * 2 / 3;
  const lngOffset = 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * Math.sqrt(Math.abs(x)) + common
    + (20 * Math.sin(x * pi) + 40 * Math.sin(x / 3 * pi) + 150 * Math.sin(x / 12 * pi) + 300 * Math.sin(x / 30 * pi)) * 2 / 3;
  const earth = 6378245, eccentricity = 0.006693421622965943;
  const rad = lat * pi / 180;
  const curvature = 1 - eccentricity * Math.sin(rad) ** 2;
  return [lat - latOffset * 180 / (earth * (1 - eccentricity) / curvature ** 1.5 * pi),
    lng - lngOffset * 180 / (earth / Math.sqrt(curvature) * Math.cos(rad) * pi)];
}

function useGcj(place: Place): boolean {
  return place.evidence === 'amap' || place.evidence === 'amap_mcp' || place.evidence === 'fixture'
    || (!place.evidence && place.source_url?.startsWith('https://uri.amap.com/') === true);
}

export function createStreetMap(node: HTMLElement, onStatus: (status: 'ready' | 'error') => void) {
  const map = L.map(node, {zoomControl: false, scrollWheelZoom: false, minZoom: 1, maxZoom: 19});
  map.attributionControl.setPrefix(false);
  const layer = L.layerGroup().addTo(map);
  const markers = new Map<string, L.Marker>();
  let bounds: L.LatLngBounds | undefined;
  let timer: number | undefined;
  let started = false;
  let destroyed = false;
  const resize = new ResizeObserver(() => map.invalidateSize({pan: false}));
  resize.observe(node);

  function showRaster() {
    if (destroyed) return;
    const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    });
    timer = window.setTimeout(() => onStatus('error'), 8_000);
    tiles.once('tileload', () => {
      window.clearTimeout(timer);
      onStatus('ready');
    });
    tiles.addTo(map);
  }

  async function showBasemap() {
    started = true;
    let vector: L.MaplibreGL | undefined;
    try {
      const [{maplibreGL}, {setWorkerUrl}] = await Promise.all([
        import('@maplibre/maplibre-gl-leaflet'), import('maplibre-gl'),
      ]);
      if (destroyed) return;
      setWorkerUrl(workerUrl);
      vector = maplibreGL({style: 'https://tiles.openfreemap.org/styles/positron'});
      vector.addTo(map);
      const vectorLayer = vector;
      timer = window.setTimeout(() => {
        vectorLayer.remove();
        showRaster();
      }, 10_000);
      vectorLayer.getMaplibreMap().once('load', () => {
        window.clearTimeout(timer);
        onStatus('ready');
      });
    } catch {
      window.clearTimeout(timer);
      if (vector) {
        if (!vector.getMaplibreMap()) {
          const incompleteLayer = vector;
          incompleteLayer.onRemove = () => {
            incompleteLayer.getContainer()?.remove();
            return incompleteLayer;
          };
        }
        vector.remove();
      }
      showRaster();
    }
  }

  function fit() {
    if (bounds?.isValid()) map.fitBounds(bounds, {padding: [52, 55], maxZoom: 16, animate: false});
  }

  return {
    fit,
    focus(id: string) {
      const marker = markers.get(id);
      if (marker) map.setView(marker.getLatLng(), 17, {animate: false});
    },
    zoomIn: () => map.zoomIn(), zoomOut: () => map.zoomOut(),
    plot(stops: Stop[], path: Coordinate[], hotel: Place | null, example: boolean, select: (id: string) => void) {
      layer.clearLayers();
      markers.clear();
      const points = [...stops, ...(hotel ? [hotel] : [])];
      const locations = points.map(point => wgsPoint([point.lng, point.lat], useGcj(point)));
      const route = path.length > 1 ? path.map(point => wgsPoint(point, stops.some(useGcj))) : locations.slice(0, stops.length);
      if (route.length > 1) L.polyline(route, {color: '#16755f', weight: 4, opacity: .82,
        dashArray: path.length > 1 && !example ? undefined : '7 7'}).addTo(layer);
      points.forEach((point, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `street-pin${index >= stops.length ? ' street-pin-hotel' : ''}`;
        button.textContent = index < stops.length ? String(index + 1) : 'H';
        button.setAttribute('aria-label', `选择 ${point.name}`);
        L.DomEvent.disableClickPropagation(button);
        button.addEventListener('click', () => select(point.poi_id));
        const title = document.createElement('span');
        title.textContent = point.name;
        const marker = L.marker(locations[index], {keyboard: false, icon: L.divIcon({html: button,
          className: 'street-marker', iconSize: [36, 36], iconAnchor: [18, 18]})}).addTo(layer);
        marker.bindTooltip(title, {direction: 'top', offset: [0, -21], className: 'street-label'});
        markers.set(point.poi_id, marker);
      });
      bounds = L.latLngBounds([...locations, ...route]);
      fit();
      if (!started && bounds.isValid()) void showBasemap();
    },
    highlight(id: string | null) {
      markers.forEach((marker, markerId) => {
        const selected = markerId === id;
        const button = marker.getElement()?.querySelector('button');
        button?.classList.toggle('street-pin-active', selected);
        button?.setAttribute('aria-pressed', String(selected));
        marker.setZIndexOffset(selected ? 500 : 0);
        if (selected) marker.openTooltip();
        else marker.closeTooltip();
      });
    },
    destroy() {
      destroyed = true;
      window.clearTimeout(timer);
      resize.disconnect();
      map.remove();
    },
  };
}
