python3 << 'PYEOF'
code = '''import pygame
import threading
import time
import math
import numpy as np
import struct
import os
from modules.map_tiles import MapTileManager, lat_lon_to_tile, tile_to_lat_lon

SCREEN_W = 480
SCREEN_H = 320
BLACK      = (0,   0,   0)
WHITE      = (255, 255, 255)
DARK_GRAY  = (30,  30,  30)
MID_GRAY   = (60,  60,  60)
LIGHT_GRAY = (180, 180, 180)
GREEN      = (0,   200, 80)
YELLOW     = (255, 200, 0)
ORANGE     = (255, 120, 0)
RED        = (220, 40,  40)
CYAN       = (0,   220, 220)
DIM_BLUE   = (20,  40,  80)
MAP_X = 0
MAP_Y = 0
MAP_W = 320
MAP_H = 320
PANEL_X = 320
PANEL_Y = 0
PANEL_W = 160
PANEL_H = 320
DEFAULT_ZOOM = 3000
ALERT_COLOURS = {"CAUTION": YELLOW, "WARNING": ORANGE, "CRITICAL": RED}

TS_MIN_X = 200
TS_MAX_X = 3900
TS_MIN_Y = 200
TS_MAX_Y = 3900

TOUCH_DEV = "/dev/input/event0"
EVENT_FMT = "llHHI"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

EV_KEY = 1
EV_ABS = 3
BTN_TOUCH = 330
ABS_X = 0
ABS_Y = 1
ABS_PRESSURE = 24


class TouchReader(threading.Thread):
    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self._running = True
        self._x = 0
        self._y = 0
        self._touching = False

    def run(self):
        try:
            fd = open(TOUCH_DEV, "rb")
        except Exception as e:
            print(f"[Touch] Cannot open {TOUCH_DEV}: {e}")
            return
        while self._running:
            try:
                data = fd.read(EVENT_SIZE)
                if not data or len(data) < EVENT_SIZE:
                    continue
                _, _, etype, ecode, evalue = struct.unpack(EVENT_FMT, data)
                if etype == EV_ABS:
                    if ecode == ABS_X:
                        raw = max(TS_MIN_X, min(TS_MAX_X, evalue))
                        self._y = int((raw - TS_MIN_X) / (TS_MAX_X - TS_MIN_X) * SCREEN_H)
                    elif ecode == ABS_Y:
                        raw = max(TS_MIN_Y, min(TS_MAX_Y, evalue))
                        self._x = int((1.0 - (raw - TS_MIN_Y) / (TS_MAX_Y - TS_MIN_Y)) * SCREEN_W)
                elif etype == EV_KEY and ecode == BTN_TOUCH:
                    if evalue == 1 and not self._touching:
                        self._touching = True
                    elif evalue == 0 and self._touching:
                        self._touching = False
                        self.callback((self._x, self._y))
            except Exception:
                pass

    def stop(self):
        self._running = False


class SkyWatchUI:
    def __init__(self, aggregator, alert_manager, proximity_engine, config):
        self.aggregator = aggregator
        self.alert_manager = alert_manager
        self.proximity = proximity_engine
        self.config = config
        self.running = False
        self.screen = None
        self.font_sm = None
        self.font_md = None
        self.font_lg = None
        self.zoom = DEFAULT_ZOOM
        self.center_lat = 0.0
        self.center_lon = 0.0
        self.device_lat = 0.0
        self.device_lon = 0.0
        self.show_settings = False
        self.selected_icao = None
        self.flash_state = True
        self.flash_timer = 0
        self.tile_manager = MapTileManager()
        self.tile_thread = None
        self._surface_cache = {}
        self._touch_queue = []
        self._touch_lock = threading.Lock()
        self._touch_reader = TouchReader(self._on_touch)
        self._touch_reader.start()

    def _on_touch(self, pos):
        with self._touch_lock:
            self._touch_queue.append(pos)

    def start(self):
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN)
        pygame.display.set_caption("SkyWatch")
        pygame.mouse.set_visible(False)
        self.font_sm = pygame.font.SysFont("monospace", 11, bold=True)
        self.font_md = pygame.font.SysFont("monospace", 13, bold=True)
        self.font_lg = pygame.font.SysFont("monospace", 15, bold=True)
        clock = pygame.time.Clock()
        self.running = True
        while self.running:
            self._handle_events()
            self._update_flash()
            self._draw()
            pygame.display.flip()
            clock.tick(10)
        pygame.quit()

    def stop(self):
        self.running = False
        self._touch_reader.stop()

    def set_position(self, lat, lon):
        self.device_lat = lat
        self.device_lon = lon
        self.center_lat = lat
        self.center_lon = lon

    def _update_tile_zoom(self):
        if self.zoom > 6000:
            self.tile_manager.zoom = 14
        elif self.zoom > 3000:
            self.tile_manager.zoom = 13
        elif self.zoom > 1500:
            self.tile_manager.zoom = 12
        elif self.zoom > 800:
            self.tile_manager.zoom = 11
        elif self.zoom > 400:
            self.tile_manager.zoom = 10
        else:
            self.tile_manager.zoom = 9
        self.tile_manager.tiles = {}
        self._surface_cache = {}
        self.tile_manager.fetch_tiles_around(self.center_lat, self.center_lon)

    def _update_flash(self):
        now = time.time()
        if now - self.flash_timer > 0.5:
            self.flash_state = not self.flash_state
            self.flash_timer = now

    def _handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                    self.zoom = min(self.zoom * 1.5, 8000)
                    self._update_tile_zoom()
                elif event.key == pygame.K_MINUS:
                    self.zoom = max(self.zoom / 1.5, 200)
                    self._update_tile_zoom()
        with self._touch_lock:
            touches = list(self._touch_queue)
            self._touch_queue.clear()
        for pos in touches:
            print(f"[Touch] tap at {pos}", flush=True)
            self._handle_touch(pos)

    def _handle_touch(self, pos):
        x, y = pos
        if self.show_settings:
            if 5 <= x <= 85 and SCREEN_H - 30 <= y <= SCREEN_H - 8:
                self.show_settings = False
            return
        if PANEL_X + 3 <= x <= PANEL_X + 77 and PANEL_H - 50 <= y <= PANEL_H - 28:
            self.zoom = min(self.zoom * 1.5, 8000)
            self._update_tile_zoom()
            return
        if PANEL_X + 82 <= x <= PANEL_X + 156 and PANEL_H - 50 <= y <= PANEL_H - 28:
            self.zoom = max(self.zoom / 1.5, 200)
            self._update_tile_zoom()
            return
        if PANEL_X + 5 <= x <= PANEL_X + PANEL_W - 5 and PANEL_H - 26 <= y <= PANEL_H - 8:
            self.show_settings = True
            return
        if x < MAP_W:
            self._select_aircraft_at(x, y)

    def _select_aircraft_at(self, touch_x, touch_y):
        traffic = self.aggregator.get_traffic()
        best_icao = None
        best_dist = 20
        for icao, ac in traffic.items():
            if ac.get("lat") and ac.get("lon"):
                sx, sy = self._latlon_to_screen(ac["lat"], ac["lon"])
                d = math.sqrt((sx - touch_x) ** 2 + (sy - touch_y) ** 2)
                if d < best_dist:
                    best_dist = d
                    best_icao = icao
        self.selected_icao = best_icao

    def _latlon_to_screen(self, lat, lon):
        if self.center_lat == 0.0 and self.center_lon == 0.0:
            return MAP_W // 2, MAP_H // 2
        dx = (lon - self.center_lon) * self.zoom
        dy = (self.center_lat - lat) * self.zoom
        sx = int(MAP_W // 2 + dx)
        sy = int(MAP_H // 2 + dy)
        return sx, sy

    def _draw(self):
        self.screen.fill(DARK_GRAY)
        if self.show_settings:
            self._draw_settings()
            return
        self._draw_map_background()
        self._draw_range_rings()
        self._draw_device_position()
        self._draw_traffic()
        self._draw_panel()
        self._draw_alert_banner()

    def _draw_map_background(self):
        pygame.draw.rect(self.screen, DIM_BLUE, (MAP_X, MAP_Y, MAP_W, MAP_H))
        if self.center_lat == 0.0 and self.center_lon == 0.0:
            return
        zoom = self.tile_manager.zoom
        n = 2 ** zoom
        cx_frac = (self.center_lon + 180.0) / 360.0 * n
        cy_frac = (1.0 - math.asinh(math.tan(math.radians(self.center_lat))) / math.pi) / 2.0 * n
        cx_int = int(cx_frac)
        cy_int = int(cy_frac)
        cx_off = int((cx_frac - cx_int) * 256)
        cy_off = int((cy_frac - cy_int) * 256)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                tx = MAP_W // 2 - cx_off + dx * 256
                ty = MAP_H // 2 - cy_off + dy * 256
                if tx + 256 < MAP_X or tx > MAP_X + MAP_W:
                    continue
                if ty + 256 < MAP_Y or ty > MAP_Y + MAP_H:
                    continue
                tile_x = cx_int + dx
                tile_y = cy_int + dy
                cache_key = (zoom, tile_x, tile_y)
                surface = self._surface_cache.get(cache_key)
                if surface is None:
                    pil_tile = self.tile_manager.get_tile(zoom, tile_x, tile_y)
                    if pil_tile:
                        try:
                            arr = np.array(pil_tile)
                            surface = pygame.surfarray.make_surface(arr.swapaxes(0, 1))
                            self._surface_cache[cache_key] = surface
                        except Exception:
                            pass
                if surface:
                    self.screen.blit(surface, (tx, ty))
        if self.tile_thread is None or not self.tile_thread.is_alive():
            self.tile_thread = threading.Thread(
                target=self.tile_manager.fetch_tiles_around,
                args=(self.center_lat, self.center_lon),
                daemon=True)
            self.tile_thread.start()

    def _draw_range_rings(self):
        cx, cy = MAP_W // 2, MAP_H // 2
        for miles in [1, 2, 3]:
            radius_px = int(miles * 1609.34 / 111320 * self.zoom)
            colour = RED if miles == 3 else (MID_GRAY if miles == 2 else LIGHT_GRAY)
            pygame.draw.circle(self.screen, colour, (cx, cy), radius_px, 1)
            label = self.font_sm.render(f"{miles}mi", True, LIGHT_GRAY)
            self.screen.blit(label, (cx + radius_px + 2, cy - 5))
        n_label = self.font_sm.render("N", True, CYAN)
        self.screen.blit(n_label, (cx - 4, cy - 22))
        pygame.draw.line(self.screen, CYAN, (cx, cy - 8), (cx, cy - 18), 1)

    def _draw_device_position(self):
        cx, cy = MAP_W // 2, MAP_H // 2
        pygame.draw.circle(self.screen, GREEN, (cx, cy), 5, 2)
        pygame.draw.line(self.screen, GREEN, (cx - 8, cy), (cx + 8, cy), 1)
        pygame.draw.line(self.screen, GREEN, (cx, cy - 8), (cx, cy + 8), 1)

    def _draw_traffic(self):
        traffic = self.aggregator.get_traffic()
        alerts = self.alert_manager.get_alerts()
        for icao, ac in traffic.items():
            lat = ac.get("lat")
            lon = ac.get("lon")
            if lat is None or lon is None:
                continue
            sx, sy = self._latlon_to_screen(lat, lon)
            if not (0 <= sx < MAP_W and 0 <= sy < MAP_H):
                continue
            source = ac.get("source", "ADSB")
            in_alert = icao in alerts
            alert_level = alerts[icao]["level"] if in_alert else None
            is_selected = icao == self.selected_icao
            if in_alert:
                colour = ALERT_COLOURS.get(alert_level, WHITE)
                if alert_level == "CRITICAL" and not self.flash_state:
                    colour = BLACK
            elif source == "REMOTEID":
                colour = ORANGE
            else:
                colour = CYAN
            if source == "REMOTEID":
                pts = [(sx, sy-5), (sx+4, sy), (sx, sy+5), (sx-4, sy)]
                pygame.draw.polygon(self.screen, colour, pts, 2)
            else:
                heading = ac.get("heading")
                if heading is not None:
                    self._draw_aircraft_arrow(sx, sy, heading, colour, is_selected)
                else:
                    pygame.draw.circle(self.screen, colour, (sx, sy), 4, 2)
            callsign = ac.get("callsign") or icao[:6]
            label = self.font_sm.render(callsign, True, colour)
            self.screen.blit(label, (sx + 5, sy - 5))
            alt = ac.get("altitude")
            if alt is not None:
                alt_label = self.font_sm.render(f"{alt//100:02d}", True, LIGHT_GRAY)
                self.screen.blit(alt_label, (sx + 5, sy + 4))
            if is_selected:
                pygame.draw.circle(self.screen, WHITE, (sx, sy), 8, 1)

    def _draw_aircraft_arrow(self, x, y, heading_deg, colour, selected):
        angle = math.radians(heading_deg - 90)
        size = 6
        tip   = (x + size * math.cos(angle),             y + size * math.sin(angle))
        left  = (x + size * 0.5 * math.cos(angle + 2.5), y + size * 0.5 * math.sin(angle + 2.5))
        right = (x + size * 0.5 * math.cos(angle - 2.5), y + size * 0.5 * math.sin(angle - 2.5))
        pygame.draw.polygon(self.screen, colour, [tip, left, right], 0 if selected else 1)

    def _draw_panel(self):
        pygame.draw.rect(self.screen, BLACK, (PANEL_X, 0, PANEL_W, PANEL_H))
        pygame.draw.line(self.screen, MID_GRAY, (PANEL_X, 0), (PANEL_X, PANEL_H), 1)
        y = 4
        title = self.font_lg.render("SkyWatch", True, CYAN)
        self.screen.blit(title, (PANEL_X + (PANEL_W - title.get_width()) // 2, y))
        y += 16
        subtitle = self.font_sm.render("Iron Ridge Systems", True, MID_GRAY)
        self.screen.blit(subtitle, (PANEL_X + (PANEL_W - subtitle.get_width()) // 2, y))
        y += 13
        pygame.draw.line(self.screen, MID_GRAY, (PANEL_X + 3, y), (PANEL_X + PANEL_W - 3, y), 1)
        y += 5
        gps = "GPS:LOCK" if self.device_lat != 0.0 else "GPS:NONE"
        gps_col = GREEN if self.device_lat != 0.0 else YELLOW
        self.screen.blit(self.font_sm.render(gps, True, gps_col), (PANEL_X + 5, y))
        y += 13
        traffic = self.aggregator.get_traffic()
        self.screen.blit(self.font_sm.render(f"TRAFFIC:{len(traffic)}", True, WHITE), (PANEL_X + 5, y))
        y += 13
        alerts = self.alert_manager.get_alerts()
        if alerts:
            col = RED if self.flash_state else DARK_GRAY
            self.screen.blit(self.font_sm.render(f"ALERT:{len(alerts)}", True, col), (PANEL_X + 5, y))
        y += 13
        pygame.draw.line(self.screen, MID_GRAY, (PANEL_X + 3, y), (PANEL_X + PANEL_W - 3, y), 1)
        y += 5
        if self.selected_icao and self.selected_icao in traffic:
            self._draw_aircraft_detail(traffic[self.selected_icao], y)
        else:
            nearest = self.alert_manager.get_highest_level()
            if nearest:
                self._draw_nearest_alert(nearest, y)
        zy = PANEL_H - 50
        zp = self.font_sm.render("ZOOM+", True, WHITE)
        zm = self.font_sm.render("ZOOM-", True, WHITE)
        pygame.draw.rect(self.screen, MID_GRAY, (PANEL_X + 3,  zy, 74, 22), 1)
        pygame.draw.rect(self.screen, MID_GRAY, (PANEL_X + 82, zy, 74, 22), 1)
        self.screen.blit(zp, (PANEL_X + 3  + (74 - zp.get_width()) // 2, zy + 5))
        self.screen.blit(zm, (PANEL_X + 82 + (74 - zm.get_width()) // 2, zy + 5))
        pygame.draw.rect(self.screen, MID_GRAY, (PANEL_X + 5, PANEL_H - 26, PANEL_W - 10, 18), 1)
        self.screen.blit(self.font_sm.render("  SETTINGS", True, LIGHT_GRAY), (PANEL_X + 8, PANEL_H - 22))

    def _draw_aircraft_detail(self, ac, y):
        self.screen.blit(self.font_md.render(ac.get("callsign") or ac.get("icao", "?"), True, CYAN), (PANEL_X + 5, y))
        y += 14
        for line in [
            f"ALT:{ac.get(\"altitude\", \"?\")}",
            f"SPD:{ac.get(\"speed\", \"?\")}",
            f"HDG:{ac.get(\"heading\", \"?\")}",
            f"SRC:{ac.get(\"source\", \"?\")}",
        ]:
            self.screen.blit(self.font_sm.render(line, True, WHITE), (PANEL_X + 5, y))
            y += 12

    def _draw_nearest_alert(self, alert, y):
        level = alert["level"]
        colour = ALERT_COLOURS.get(level, WHITE)
        self.screen.blit(self.font_md.render(f"!{level}!", True, colour), (PANEL_X + 5, y))
        y += 14
        for line in [
            alert.get("callsign", "?"),
            f"{alert.get(\"distance_miles\", \"?\")}mi {alert.get(\"bearing\", \"\")}",
            f"ALT:{alert.get(\"altitude\", \"?\")}",
            f"SRC:{alert.get(\"source\", \"?\")}",
        ]:
            self.screen.blit(self.font_sm.render(str(line), True, WHITE), (PANEL_X + 5, y))
            y += 12

    def _draw_alert_banner(self):
        alerts = self.alert_manager.get_alerts()
        critical = [a for a in alerts.values() if a["level"] == "CRITICAL"]
        if not critical or not self.flash_state:
            return
        a = critical[0]
        banner = pygame.Surface((MAP_W, 40), pygame.SRCALPHA)
        banner.fill((220, 40, 40, 210))
        self.screen.blit(banner, (0, MAP_H // 2 - 20))
        msg = f"CRIT:{a[\"callsign\"]} {a[\"distance_miles\"]}mi {a[\"bearing\"]}"
        self.screen.blit(self.font_md.render(msg, True, WHITE), (5, MAP_H // 2 - 8))

    def _draw_settings(self):
        self.screen.fill(BLACK)
        y = 5
        self.screen.blit(self.font_lg.render("SETTINGS", True, CYAN), (5, y))
        y += 20
        audio_on = self.config.get("audio_enabled", True)
        for item in [
            f"Audio: {\"ON\" if audio_on else \"OFF\"}",
            f"Alert: {self.config.get(\"alert_radius_miles\", 3.0)}mi",
            f"Warn:  {self.config.get(\"warning_radius_miles\", 1.5)}mi",
            f"Crit:  {self.config.get(\"critical_radius_miles\", 0.5)}mi",
        ]:
            self.screen.blit(self.font_sm.render(item, True, WHITE), (5, y))
            y += 18
        pygame.draw.rect(self.screen, MID_GRAY, (5, SCREEN_H - 30, 80, 22), 1)
        self.screen.blit(self.font_sm.render("< BACK", True, WHITE), (10, SCREEN_H - 25))
'''
with open('/home/pi/skywatch/modules/ui.py', 'w') as f:
    f.write(code)
print("Done - wrote", len(code), "bytes")
PYEOF