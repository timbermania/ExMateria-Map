-- exmateria-map: the Lua handlers the Blender live link needs.
--
--     pcsx-redux -webserver -webserver-port 8080 -dofile pcsx_handlers.lua
--
-- Load this into a **stock, released PCSX-Redux**. Nothing here is fork-only,
-- and nothing here may become fork-only: the moment a handler calls
-- `PCSX.getLuaConsole`, `PCSX.SPU.*` or any other binding that exists only in
-- `timbermania/pcsx-redux`, an artist's unmodified emulator stops answering
-- and the whole point of shipping this file is gone.
--
-- Three routes get this file loaded, and the addon's preferences offer all
-- three (`MAP_OT_launch_pcsx`, `MAP_OT_setup_pcsx`, `MAP_OT_copy_launch_command`):
--
--   1. Blender starts the emulator with `-dofile` on the command line.
--   2. A `pcsx.lua` in the emulator's WORKING DIRECTORY that `dofile`s this.
--      Its GUI Lua editor reads that file at startup and runs it on the pane's
--      first draw (`Auto run` is on by default) -- so a plain double-click
--      loads the handlers with no flags at all. **The pane must be visible**:
--      `draw()` is what runs the buffer, and it is called only when
--      *Show Lua editor* is ticked, which persists in the emulator's
--      `pcsx.json`. Measured both ways -- with it off, `cpu/ram` answers 200
--      and `lua/ping` is a 404.
--   3. `-dofile` typed by hand, or this file pasted into that same Lua editor.
--
-- Route 2 is why `-exec` and the archive `autoexec.lua` are not used: they are
-- launch-time only, and the point of route 2 is that there is no launch line.
--
-- Everything else the addon needs is an upstream HTTP endpoint and needs no
-- handler at all: main RAM is `GET`/`POST /api/v1/cpu/ram/raw` and VRAM is
-- `GET`/`POST /api/v1/gpu/vram/raw`. This file exists for the one thing no
-- endpoint reaches -- the GTE control registers, which are cop2 state and not
-- `m_wram` -- plus a `ping` cheap enough to gate a push on.
--
-- TRAP 1: `PCSX.WebServer` is nil at `-dofile` time. Create the tables here or
-- the handler assignment below raises and the emulator comes up with no
-- handlers registered and no obvious reason why.
--
-- TRAP 2: on stock, a handler receives its payload **only through the URL**.
-- A POST body is not exposed to Lua (`req.body` does not exist), an urlencoded
-- POST arrives with `req.form` empty, and a multipart POST puts the part
-- *headers* in `req.form` and concatenates the values without boundaries. So
-- every handler here reads `req.urlData.query` and nothing else.
--
-- TRAP 3: the URL is capped at 252 bytes and overflowing it is a **silent
-- 404**, not an error. `BUFFER_SIZE = 256` in `src/core/web-server.cc` and
-- `onUrl` parses each read chunk as a whole URI instead of accumulating, so a
-- longer URL resolves to a path that does not exist. Bisected on a live
-- emulator: a 251-byte URL runs the handler, a 257-byte URL is
-- `404 URL Not found`. The Python side chunks its writes to stay under it.

PCSX.WebServer = PCSX.WebServer or {}
PCSX.WebServer.Handlers = PCSX.WebServer.Handlers or {}
local H = PCSX.WebServer.Handlers

local function ok(b)
  return "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
      .. "Content-Length: " .. #b .. "\r\n\r\n" .. b
end

--- `GET /api/v1/lua/ping` -> `pong`. The push's first gate: an emulator that
--- is not there costs two seconds to find out about, and assembling the
--- document costs more.
H.ping = function(req) return ok("pong\n") end

--- `GET /api/v1/lua/gte?<index>=<u32>&...` -> `<how many were written>`.
---
--- The light rig's GTE half. `PCSX.getRegisters().CP2C` is the cop2 control
--- file: `cnt13-15` is the back colour (ambient) and `cnt16-20` the light
--- colour matrix (the per-state gains). They are not main RAM, so no HTTP
--- endpoint reaches them and this handler is the only route.
---
--- The count comes back so the caller can tell a dropped pair from a
--- delivered one: a value that does not match `%d+` -- a negative, a hex
--- literal -- is skipped here in silence, and a caller comparing the count
--- against what it sent turns that silence into an error.
H.gte = function(req)
  local r = PCSX.getRegisters()
  local n = 0
  for k, v in (req.urlData.query or ""):gmatch("(%d+)=(%d+)") do
    local i = tonumber(k)
    if i >= 0 and i <= 31 then r.CP2C.r[i] = tonumber(v); n = n + 1 end
  end
  return ok(tostring(n) .. "\n")
end
