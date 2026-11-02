-- Applies a one-shot teleport requested by the Python plugin via ac.ext_storeLua.
local applied = 0

function script.update()
  local id = ac.load("acai.spawn_id") or 0
  if id == 0 or id == applied then
    return
  end
  local pos = vec3(ac.load("acai.spawn_x") or 0, ac.load("acai.spawn_y") or 0, ac.load("acai.spawn_z") or 0)
  local dir = vec3(ac.load("acai.dir_x") or 0, ac.load("acai.dir_y") or 0, ac.load("acai.dir_z") or 1)
  if math.abs(pos.x) < 1 and math.abs(pos.z) < 1 then
    return
  end
  applied = id
  physics.setCarVelocity(0, vec3(0, 0, 0))
  physics.setCarPosition(0, pos, dir)
end
