"""Source links, public admission tariffs and optional Booking.com quote lookups."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import unicodedata
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .domain import ProviderError, TripRequest


BOOKING_URL = "https://demandapi.booking.com/3.3"
BOOKING_NAME = "Booking.com"
BOOKING_HOTEL_RADIUS_KM = 7
CNY = "CNY"
USD = "USD"
SEATTLE_NAMES = {"西雅图", "美国西雅图", "seattle", "seattlewa", "seattlewashington", "seattleus", "seattleusa",
                 "seattlewaus", "seattlewashingtonus", "seattlewausa", "seattlewashingtonusa"}
SEATTLE_ART_MUSEUM = "https://seattleartmuseum.org/visit/seattle-art-museum"
SEATTLE_TOURISM = "https://visitseattle.org/press/press-kit/seattle-on-a-budget/"
SHANGHAI_TOURISM = "https://cmp.whlyj.sh.gov.cn/CMP/news_view.ac?id=b02d8bc0f56d4f15827bd50b87989437"
SHANGHAI_TICKETS = {
    "东方明珠广播电视塔": (199, SHANGHAI_TOURISM, "上海市文化和旅游局", "二球观光普通成人票。"),
    "上海自然博物馆": (30, "https://nao.snhm.org.cn/foreign/cgfw/cgzx.htm", "上海自然博物馆官网", "普通成人参观票。"),
    "静安寺": (50, "https://www.cnta-osaka.jp/PDF/%E4%B8%8A%E6%B5%B7%E5%B0%8F%E7%94%BA-2025.pdf", "中国驻大阪旅游办事处", "普通日香花券，特殊节日以寺方公示为准。"),
    "上海四行仓库抗战纪念馆": (0, "https://whlyj.sh.gov.cn/wbzx/20250908/4f0d28712baa431fa301f85fb3083875.html", "上海市文化和旅游局", "纪念馆参观免费，开放和预约规则以馆方公示为准。"),
}
SHANGHAI_OPEN_SPACES = {
    "外滩": "https://www.shanghai.gov.cn/nw4411/20261004/1c0ab0f5b0c04dea8f2e06f3017ef4d8.html",
    "南京路步行街": "https://www.shanghai.gov.cn/huangpu/index.html",
    "上海人民广场": "https://www.shanghai.gov.cn/huangpu/index.html",
    "上海新天地": "https://www.shanghai.gov.cn/huangpu/index.html",
}
XIAN_MUSEUMS = "https://wlj.xa.gov.cn/wbdw/bwgjs/1.html"
XIAN_BELL_DRUM = "https://wwj.xa.gov.cn/xwzx/tzgg/2105187558014095361.html"
XIAN_TICKETS = {
    "陕西历史博物馆": (0, "https://www.sxhm.com/guide.h", "陕西历史博物馆官网", "本馆基本陈列免费，需实名预约。收费特展另计。"),
    "陕西省美术博物馆": (0, "https://whhlyt.shaanxi.gov.cn/zfxxgk/fdzdgknr/ysjs/202509/P020250912419167910040.pdf", "陕西省文化和旅游厅", "常规参观免费，当期展览和入馆安排以馆方公告为准。"),
    "西安城市运动公园": (0, "https://xetdz.xa.gov.cn/zwgk/ggwhfw/2046042459314712578.html", "西安经开区", "公园公共区域免费，部分运动场馆或设施可能收费。"),
    "文景公园": (0, "https://xetdz.xa.gov.cn/xwzx/jkdt/1784884710014013442.html", "西安经开区", "公益公园公共区域免费。"),
    "西安博物院": (0, XIAN_MUSEUMS, "西安市文化和旅游局", "常规参观免费，一般周二闭馆。"),
    "西安碑林博物馆": (85, "https://www.beilin-museum.com/index.php?a=index&aid=4627&c=View&m=home", "西安碑林博物馆官网", "普通成人全票，需实名预约购票。"),
    "西安城墙": (54, "https://qjxq.xa.gov.cn/zwgk/ndzdxxgk/jytabl/1958091978013503489.html", "西安曲江新区管理委员会", "普通成人城墙单票，单独参观碑林博物馆另计。"),
    "西安钟楼": (30, XIAN_BELL_DRUM, "西安市钟鼓楼博物馆", "普通成人钟楼单票，施工期间执行临时票价。"),
    "西安鼓楼": (30, XIAN_BELL_DRUM, "西安市钟鼓楼博物馆", "普通成人鼓楼单票，施工期间执行临时票价。"),
    "西安半坡博物馆": (55, "https://www.banpomuseum.com.cn/index.php?a=lists&c=index&catid=8&kid=29&m=content",
                    "西安半坡博物馆官网", "普通成人参观票，3–11 月 ¥55，12–2 月 ¥40。"),
    "巧克巧蔻·巧克力博物馆(西安大唐不夜城馆)": (None, "https://you.ctrip.com/sight/xian7/145186573.html", "携程景点页", "请按参观日期查看成人入馆票，商品和体验套餐价格另计。"),
}
XIAN_TICKET_ALIASES = {"钟楼": "西安钟楼", "鼓楼": "西安鼓楼", "半坡博物馆": "西安半坡博物馆",
                       "西安城墙·碑林历史文化景区": "西安城墙"}
NANJING_PRICE_GUIDE = "https://fgw.nanjing.gov.cn/njsfzhggwyh/202601/P020260107586054627681.pdf"
NANJING_TICKETS = {
    "南京博物院": (0, "https://www.njmuseum.org.cn/zh/visitIndex", "南京博物院官网", "基本陈列免费，所有参观者须预约。收费临展另计。"),
    "红山森林动物园": (70, "https://fgw.nanjing.gov.cn/njsfzhggwyh/202301/t20230109_3797478.html", "南京市发展和改革委员会", "自 2025 年 10 月 18 日起的普通成人票价。免费开放活动须按园方公告预约。"),
    "玄武湖": (0, "https://www.xuanwuhu.net/index.aspx", "玄武湖景区官网", "五洲公共区域免费开放。游船和收费项目另计。"),
    "鸡鸣寺": (10, NANJING_PRICE_GUIDE, "南京市发展和改革委员会 2026 年价格手册", "普通香花券按公示的政府指导价 ¥10 估算，特殊活动请核对寺方公告。"),
    "南京城墙北线": (30, NANJING_PRICE_GUIDE, "南京市发展和改革委员会 2026 年价格手册", "神策门至太平门段普通登城票包含台城。连续游览同一段按一次入场计。"),
}
NANJING_TICKET_ALIASES = {
    "南京红山森林动物园": "红山森林动物园", "玄武湖景区": "玄武湖", "玄武湖公园": "玄武湖",
    "古鸡鸣寺": "鸡鸣寺", "南京鸡鸣寺": "鸡鸣寺",
    "南京城墙景区(神策门至太平门)": "南京城墙北线", "南京城墙台城景区": "南京城墙北线",
}


def _seattle(city: str) -> bool:
    return _name_key(city) in SEATTLE_NAMES


def public_transit_reference(city: str) -> list[dict]:
    """Return a city's published adult rates independently of any estimated route."""
    if not _seattle(city):
        return []
    return [
        {"label": "King County Metro 公交成人单程", "currency": USD, "amount": 3,
         "source_url": "https://kingcounty.gov/en/dept/metro/fares-and-payment/prices", "source_name": "King County Metro",
         "notes": ["适用于普通成人票。转乘、折扣和其他交通服务按运营方规定计费。"]},
        {"label": "Link 轻轨成人单程", "currency": USD, "amount": 3,
         "source_url": "https://www.soundtransit.org/ride-with-us/how-to-pay/fares?tab=2", "source_name": "Sound Transit",
         "notes": ["适用于 1 号线和 2 号线普通成人票。"]},
    ]


def booking_available() -> bool:
    return bool(os.getenv("BOOKING_API_TOKEN", "").strip() and os.getenv("BOOKING_AFFILIATE_ID", "").strip().isdigit())


def amap_source(name: str, lng: float, lat: float) -> dict:
    query = urlencode({"position": f"{lng:.6f},{lat:.6f}", "name": name, "coordinate": "gaode"})
    return {"source_url": f"https://uri.amap.com/marker?{query}", "source_name": "高德地图"}


def lodging_scope(request: TripRequest) -> tuple[int, int]:
    return max(0, request.days - 1), math.ceil(request.travelers / 2)


def lodging_search(request: TripRequest) -> dict:
    nights, rooms = lodging_scope(request)
    if not nights:
        return {}
    query = urlencode({
        "ss": request.city, "checkin": request.start_date.isoformat(), "checkout": request.dates()[-1],
        "group_adults": request.travelers, "no_rooms": rooms, "group_children": 0, "selected_currency": CNY,
    })
    return {"source_url": f"https://www.booking.com/searchresults.zh-cn.html?{query}", "source_name": "Booking.com 城市住宿搜索"}


def sample_admission(place: dict) -> dict:
    published = _official_tariff(place)
    if published:
        published.pop("offpeak_amount", None)
        return published
    amounts = {"园林": 40, "文博建筑群": 35, "动物园": 60, "科技馆": 30, "展览馆": 25, "美术馆": 20}
    return {"status": "sample", "currency": CNY, "amount": amounts.get(place.get("category"), 0),
            "notes": ["示例门票估算，实际票价请以场馆公示为准。"]}


def sample_lodging(request: TripRequest, room_rate: int) -> dict:
    nights, rooms = lodging_scope(request)
    dates = [date.fromisoformat(day) for day in request.dates()[:-1]]
    per_night = [{"date": day.isoformat(), "amount": (room_rate + (30 if day.weekday() in (4, 5) else 0)) * rooms}
                 for day in dates]
    total = sum(row["amount"] for row in per_night)
    return {"status": "sample", "currency": CNY, "nightly": _nightly_average(total, nights), "total": total,
            "nights": nights, "rooms": rooms, "per_night": per_night,
            "notes": ["示例房价，按每间房最多两人估算。每晚金额为全部房间的合计。"]}


def _official_tariff(place: dict) -> dict | None:
    if _seattle(str(place.get("city", ""))):
        return _seattle_admission(str(place.get("name", "")))
    key = (str(place.get("city", "")).removesuffix("市"), place.get("name"))
    notes = ["按官网公示的普通成人票价计算。出行前请核对预约和开放情况。"]
    if key[0] == "西安":
        name = XIAN_TICKET_ALIASES.get(key[1], key[1])
        if reference := XIAN_TICKETS.get(name):
            amount, url, source, detail = reference
            status = "unknown" if amount is None else "free" if amount == 0 else "quoted"
            return {"status": status, "currency": CNY, "amount": amount, "source_url": url,
                    "source_name": source, "notes": [detail]}
    if key[0] == "南京":
        name = NANJING_TICKET_ALIASES.get(key[1], key[1])
        if name == "环洲" and "玄武湖" in (place.get("address") or ""):
            name = "玄武湖"
        if reference := NANJING_TICKETS.get(name):
            amount, url, source, detail = reference
            quote = {"status": "quoted" if amount else "free", "currency": CNY, "amount": amount,
                     "source_url": url, "source_name": source, "notes": [detail], "evidence": "official_tariff"}
            if name == "鸡鸣寺":
                quote["price_kind"] = "reference"
            if name == "南京城墙北线":
                quote["admission_group"] = "nanjing-city-wall-north"
            return quote
    if key in (("北京", "故宫博物院"), ("北京", "故宫")):
        return {"status": "quoted", "currency": CNY, "amount": 60, "offpeak_amount": 40,
                "source_url": "https://www.dpm.org.cn/subject_booking/", "source_name": "故宫博物院官网",
                "notes": [*notes, "珍宝馆和钟表馆各需另购 ¥10 门票。"]}
    if key in (("上海", "豫园"), ("上海", "上海豫园")):
        return {"status": "quoted", "currency": CNY, "amount": 40, "offpeak_amount": 30,
                "source_url": "https://www.yugarden.com.cn/Page/WxyGarden/notice.html", "source_name": "豫园管理处",
                "notes": [*notes, "适用于豫园日间入园。夜游和灯会单独计价。"]}
    if key[0] == "上海" and _people_square_museum(place):
        return {"status": "unknown", "source_url": "https://www.shanghaimuseum.cn/mu/frontend/pg/service/visit-west",
                "source_name": "上海博物馆官网", "notes": ["人民广场馆的入场价格以对应展览购票页面为准。"]}
    if key[0] == "上海" and (reference := SHANGHAI_TICKETS.get(key[1])):
        amount, url, source, detail = reference
        return {"status": "quoted" if amount else "free", "currency": CNY, "amount": amount, "source_url": url,
                "source_name": source, "notes": [detail]}
    if key[0] == "上海" and (url := SHANGHAI_OPEN_SPACES.get(key[1])):
        return {"status": "free", "currency": CNY, "amount": 0, "source_url": url, "source_name": "上海市人民政府",
                "notes": ["公共区域免费开放，周边场馆、商户和活动另计。"]}
    if key == ("成都", "成都大熊猫繁育研究基地"):
        return {"status": "quoted", "currency": CNY, "amount": 55,
                "source_url": "https://www.panda.org.cn/yd/service/ticket/", "source_name": "成都熊猫基地官网",
                "notes": notes}
    return None


def _seattle_admission(name: str) -> dict | None:
    key = _name_key(name.split("·")[-1])
    if key in {"mopop", "museumofpopculture", "流行文化博物馆"}:
        return {"status": "unknown", "currency": USD, "source_url": "https://www.mopop.org/tickets",
                "source_name": "MoPOP 官方票务", "notes": ["普通门票随日期和时段浮动。请在官网日历查看。"]}
    if key in {"seattleartmuseum", "西雅图艺术博物馆"}:
        return {"status": "quoted", "currency": USD, "amount": 29.99, "source_url": SEATTLE_ART_MUSEUM,
                "source_name": "Seattle Art Museum 官网", "notes": ["市中心馆普通成人票。每月首个星期四免费开放。"]}
    if key in {"chihulygardenandglass", "chihulygardenglass", "奇胡利玻璃艺术园"}:
        return {"status": "quoted", "currency": USD, "amount": 40, "maximum": 45, "price_kind": "range",
                "source_url": "https://www.chihulygardenandglass.com/visit/plan-your-visit", "source_name": "Chihuly Garden and Glass 官网",
                "notes": ["官网公示的普通入场票价格区间。入场税和场地费另计，具体时段价格见官网。"]}
    if key in {"olympicsculpturepark", "奥林匹克雕塑公园"}:
        return {"status": "free", "currency": USD, "amount": 0,
                "source_url": "https://www.seattleartmuseum.org/visit/olympic-sculpture-park/", "source_name": "Seattle Art Museum 官网",
                "notes": ["公园免费开放，车库停车另收费。"]}
    if key in {"discoverypark", "kerrypark", "探索公园", "凯瑞公园"}:
        return {"status": "free", "currency": USD, "amount": 0, "source_url": SEATTLE_TOURISM,
                "source_name": "Visit Seattle 官方旅游指南", "notes": ["公园免费入场。"]}
    if key in {"pikeplacemarket", "派克市场"}:
        return {"status": "free", "currency": USD, "amount": 0,
                "source_url": "https://www.pikeplacemarket.org/ufaq/is-there-an-admission-fee-to-pike-place-market/",
                "source_name": "Pike Place Market 官网", "notes": ["市场免费进入，购物和餐饮另计。"]}
    return None


def _people_square_museum(place: dict) -> bool:
    name = str(place.get("name", ""))
    if "上海博物馆" in name and "人民广场馆" in name:
        return True
    return name == "上海博物馆" and abs(place.get("lng", 0) - 121.4753) < 0.01 and abs(place.get("lat", 0) - 31.2303) < 0.01


def official_admission(place: dict, visit_date: str) -> dict | None:
    tariff = _official_tariff(place)
    if tariff is None:
        return None
    offpeak = tariff.pop("offpeak_amount", None)
    day = date.fromisoformat(visit_date)
    month = day.month
    source = tariff["source_name"]
    if source == "西安碑林博物馆官网" and not date(2025, 5, 12) <= day < date(2027, 5, 12):
        tariff.update(status="unknown", amount=None, notes=["请以参观日适用的最新票价公告为准。"])
    if source == "西安市钟鼓楼博物馆":
        name = XIAN_TICKET_ALIASES.get(place["name"], place["name"])
        start, end = (date(2026, 10, 11), date(2026, 11, 15)) if name == "西安钟楼" else (date(2026, 11, 1), date(2026, 11, 30))
        if start <= day <= end:
            tariff.update(amount=15, notes=["施工期间观景平台临时票，楼内展厅以馆方公告为准。"])
    if source == "西安半坡博物馆官网" and month in (12, 1, 2):
        tariff["amount"] = 40
    if tariff["source_name"] == "故宫博物院官网" and month not in range(4, 11):
        tariff["amount"] = offpeak
    if tariff["source_name"] == "豫园管理处" and month not in (4, 5, 6, 9, 10, 11):
        tariff["amount"] = offpeak
    if tariff["source_name"] == "上海博物馆官网" and date(2026, 7, 9) <= day <= date(2027, 11, 14):
        tariff.update(status="quoted", amount=148, currency=CNY,
                      source_url="https://whlyj.sh.gov.cn/wbzx/20260920/4c97f524ffb84d519eb9294b3739bee0.html",
                      source_name="上海市文化和旅游局",
                      notes=["人民广场馆「世界树之巅」特展公示参考票价。优惠、预约及余票请核对官方渠道。"])
    if tariff["source_url"] == SEATTLE_ART_MUSEUM and day.day <= 7 and day.weekday() == 3:
        tariff.update(status="free", amount=0, notes=["每月首个星期四普通入场免费。请在官网确认预约安排。"])
    if ((tariff["source_url"] == NANJING_PRICE_GUIDE and day.year != 2026)
            or (place.get("name") in {"红山森林动物园", "南京红山森林动物园"} and day < date(2025, 10, 18))):
        tariff.update(status="unknown", amount=None, notes=["请查看参观日期适用的最新票价。"])
    tariff["visit_date"] = visit_date
    return tariff


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    localized = _dict(value)
    for key in ("zh-cn", "en-gb"):
        if isinstance(localized.get(key), str) and localized[key].strip():
            return localized[key].strip()
    return ""


def _amount(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _nightly_average(total: float, nights: int) -> float:
    nightly = Decimal(str(total)) / nights if nights else Decimal(0)
    return float(nightly.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _coordinates(location: dict) -> tuple[float, float] | None:
    coordinates = _dict(location.get("coordinates"))
    lng, lat = coordinates.get("longitude"), coordinates.get("latitude")
    if type(lng) in (float, int) and type(lat) in (float, int) and -180 <= lng <= 180 and -90 <= lat <= 90:
        return lng, lat
    return None


def _safe_link(value: object, prefix: str) -> str:
    if not isinstance(value, str) or any(character.isspace() or character == "\\" for character in value):
        return ""
    try:
        link = urlsplit(value)
        if link.scheme == "https" and link.netloc.lower() in {"booking.com", "www.booking.com"} and link.path.startswith(prefix):
            return value
    except ValueError:
        pass
    return ""


def _hotel_price(row: dict) -> float | None:
    price = _dict(row.get("price")).get("total")
    currency = row.get("currency")
    if currency == CNY:
        return _amount(price)
    amounts, currencies = _dict(price), _dict(currency)
    if currencies.get("booker") == CNY and (amount := _amount(amounts.get("booker_currency"))) is not None:
        return amount
    if currencies.get("accommodation") == CNY:
        return _amount(amounts.get("accommodation_currency"))
    return None


def _name_key(name: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKC", name).casefold() if character.isalnum())


def _is_admission_title(place_name: str, title: str) -> bool:
    venue, product = _name_key(place_name), _name_key(title)
    return bool(venue and product in {f"{venue}{suffix}" for suffix in (
        "门票", "成人门票", "门票成人", "入场券", "入场门票", "成人入场券", "入场券成人", "通用门票",
        "admissionticket", "entryticket", "entranceticket",
    )})


def _near(place: dict, location: dict) -> bool:
    if location.get("country") != "cn" or location.get("type") != "attraction":
        return False
    point = _coordinates(location)
    if point is None:
        return False
    # Booking's GPS and AMap coordinates may differ by several hundred metres.
    return _distance_km(place, point) <= 1.5


def _distance_km(anchor: dict, point: tuple[float, float]) -> float:
    lng, lat = point
    lon1, lat1, lon2, lat2 = map(math.radians, (anchor["lng"], anchor["lat"], lng, lat))
    span = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12_742 * math.asin(min(1, math.sqrt(span)))


class BookingPricing:
    """Resolve only provider-supplied prices whose currency and product type are known."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def _post(self, endpoint: str, params: dict) -> list[dict]:
        headers = {"Authorization": f"Bearer {os.environ['BOOKING_API_TOKEN'].strip()}",
                   "X-Affiliate-Id": os.environ["BOOKING_AFFILIATE_ID"].strip()}
        try:
            response = self.client.post(f"{BOOKING_URL}/{endpoint}", json=params, headers=headers, timeout=8)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("booking_unavailable", "Booking.com 当前无法返回报价") from exc
        return _items(_dict(data).get("data"))

    @staticmethod
    def clock() -> datetime:
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    def hotels(self, request: TripRequest, anchor: dict, *, country: str | None = "cn") -> list[dict]:
        now = self.clock()
        if country == "us" and _seattle(request.city):
            now = now.astimezone(ZoneInfo("America/Los_Angeles"))
        nights, rooms = lodging_scope(request)
        if not booking_available() or not nights or request.start_date < now.date():
            return []
        checkin, checkout = request.dates()[0], request.dates()[-1]
        if (date.fromisoformat(checkout) - now.date()).days > 500:
            return []
        rows = self._post("accommodations/search", {
            "booker": {"country": "cn", "platform": "desktop"}, "checkin": checkin, "checkout": checkout,
            "coordinates": {"latitude": anchor["lat"], "longitude": anchor["lng"], "radius": BOOKING_HOTEL_RADIUS_KM},
            "currency": CNY, "guests": {"number_of_adults": request.travelers, "number_of_rooms": rooms},
            "rows": 10, "sort": {"by": "price", "direction": "ascending"},
        })
        valid = {row["id"]: (amount, url) for row in rows if type(row.get("id")) is int
                 and (amount := _hotel_price(row)) is not None and (url := _safe_link(row.get("url"), "/hotel/"))}
        if not valid:
            return []
        ids = sorted(valid, key=lambda item: valid[item][0])[:5]
        details = self._post("accommodations/details", {"accommodations": ids, "languages": ["zh-cn", "en-gb"]})
        results = {}
        for row in details:
            location = _dict(row.get("location"))
            coordinates = _coordinates(location)
            identifier, name = row.get("id"), _text(row.get("name"))
            hotel_country = _text(location.get("country")).lower()
            if type(identifier) is not int or identifier not in ids or not coordinates or not name or not hotel_country:
                continue
            if country is not None and hotel_country != country.lower():
                continue
            # Domestic anchors use GCJ-02, so allow for its offset from the property's GPS coordinates.
            radius = BOOKING_HOTEL_RADIUS_KM + (1 if hotel_country == "cn" else 0)
            if _distance_km(anchor, coordinates) > radius:
                continue
            total, url = valid[identifier]
            results[identifier] = {
                "poi_id": f"booking-{identifier}", "name": name, "address": _text(location.get("address")),
                "lng": coordinates[0], "lat": coordinates[1], "category": "酒店", "indoor": True, "country": hotel_country,
                "evidence": "booking", "source_url": url, "source_name": BOOKING_NAME,
                "pricing": {"status": "quoted", "currency": CNY, "total": total,
                            "nightly": _nightly_average(total, nights), "nights": nights, "rooms": rooms,
                            "source_url": url, "source_name": BOOKING_NAME, "observed_at": now.isoformat(),
                            "notes": ["按所选日期、人数和房间数查询的整段入住报价。",
                                      "平台仅返回整段报价，每晚显示全部房间的平均价。实际付款以订房页面为准。"]},
            }
        return [results[identifier] for identifier in ids if identifier in results]

    def attractions(self, visits: list[tuple[dict, str]]) -> dict:
        result = {}
        now = self.clock()
        by_date = defaultdict(list)
        for place, visit_date in visits:
            tariff = official_admission(place, visit_date)
            if tariff:
                result[place["poi_id"]] = tariff
            elif 0 <= (date.fromisoformat(visit_date) - now.date()).days <= 180:
                by_date[visit_date].append(place)
        if not booking_available():
            return result
        for visit_date, locations in by_date.items():
            try:
                result.update(self._admissions_for_date(locations, visit_date, now.isoformat()))
            except ProviderError:
                # Booking is optional. Keep every map link and any official venue tariff.
                break
        return result

    def _admissions_for_date(self, places: list[dict], visit_date: str, observed_at: str) -> dict:
        anchor = places[0]
        rows = self._post("attractions/search", {
            "currency": CNY, "dates": {"start_date": visit_date, "end_date": visit_date},
            "coordinates": {"latitude": anchor["lat"], "longitude": anchor["lng"], "radius": 12}, "rows": 30,
        })
        quotes = {}
        for row in rows:
            urls = _dict(_dict(row.get("urls")).get("web"))
            identifier, price = row.get("id"), _dict(row.get("price"))
            if (isinstance(identifier, str) and price.get("currency") == CNY
                    and (amount := _amount(price.get("total"))) is not None
                    and (url := _safe_link(urls.get("detail"), "/attractions/"))):
                quotes[identifier] = (amount, url)
        if not quotes:
            return {}
        details = self._post("attractions/details", {"attractions": list(quotes), "languages": ["zh-cn"]})
        result = {}
        for row in details:
            identifier, title, locations = row.get("id"), _text(row.get("name")), _items(row.get("locations"))
            if not isinstance(identifier, str) or identifier not in quotes:
                continue
            amount, url = quotes[identifier]
            for place in places:
                previous = result.get(place["poi_id"])
                if previous is not None and amount >= previous["amount"]:
                    continue
                if _is_admission_title(place["name"], title) and any(_near(place, location) for location in locations):
                    result[place["poi_id"]] = {
                        "status": "quoted", "currency": CNY, "amount": amount, "source_url": url,
                        "source_name": BOOKING_NAME, "observed_at": observed_at, "visit_date": visit_date,
                        "notes": ["按当日可查的成人普通入场票起价显示。实际价格和余票以链接页面为准。"],
                    }
        return result
