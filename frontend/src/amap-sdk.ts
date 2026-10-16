export interface AmapMap {
  add(overlays: unknown[]): void
  clearMap(): void
  setFitView(): void
  destroy(): void
  on(event: string, handler: () => void): void
  off(event: string, handler: () => void): void
}
interface AmapSdk {
  Map: new (element: HTMLElement, options: Record<string,unknown>) => AmapMap
  Marker: new (options: Record<string,unknown>) => unknown
  Polyline: new (options: Record<string,unknown>) => unknown
}
declare global {
  interface Window {
    AMap?: AmapSdk
    _AMapSecurityConfig?: {serviceHost: string}
    __travelAmapReady?: () => void
  }
}
let sdkPromise: Promise<AmapSdk> | null = null
let sdkKey = ''

export function loadAmap(key: string): Promise<AmapSdk> {
  if (sdkKey && sdkKey !== key) return Promise.reject(new Error('网页地图 Key 已更换，请刷新页面后查看地图'))
  if (sdkPromise) return sdkPromise
  sdkKey = key
  window._AMapSecurityConfig = {serviceHost:window.location.origin + '/_AMapService'}
  sdkPromise = new Promise<AmapSdk>((resolve,reject) => {
    const script = document.createElement('script')
    const clean = () => {clearTimeout(timer);delete window.__travelAmapReady;script.onerror = null}
    const fail = () => {clean();script.remove();reject(new Error('高德网页地图加载失败，请检查网络、JSAPI Key 和域名白名单'))}
    const timer = window.setTimeout(fail,25000)
    window.__travelAmapReady = () => {
      clean()
      if (window.AMap) resolve(window.AMap)
      else reject(new Error('高德地图组件未就绪，请重试'))
    }
    script.async = true
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}&callback=__travelAmapReady`
    script.onerror = fail
    document.head.appendChild(script)
  }).catch(error => {sdkPromise = null;sdkKey = '';throw error})
  return sdkPromise
}
