"""一次性只读探测：核对 Zmotion 控制器四轴现状，不移动任何轴。"""
import sys
sys.path.insert(0, '.')

from tuner.zmotion import ZmotionController, ZmotionError

IP = '192.168.0.11'

c = ZmotionController()
try:
    c.open(IP, retries=2)
except ZmotionError as exc:
    print('OPEN FAILED:', exc)
    sys.exit(1)

print('connected :', c.connected)
print('firmware  :', repr(c.firmware))

for ax in range(4):
    print(f'--- axis {ax} ---')
    for name, fn in (('atype', c.get_atype), ('units', c.get_units),
                     ('speed', c.get_speed), ('datum_in', c.get_datum_in),
                     ('dpos', c.get_dpos), ('mpos', c.get_mpos)):
        try:
            print(f'  {name:9s}: {fn(ax)}')
        except Exception as e:
            print(f'  {name:9s}: ERR {e!r}')
    # homing / alarm status (method names may vary across firmware)
    for name in ('home_status', 'axis_status', 'get_home_status', 'get_axis_status'):
        fn = getattr(c, name, None)
        if fn is None:
            continue
        try:
            print(f'  {name:13s}: {fn(ax)}')
        except Exception as e:
            print(f'  {name:13s}: ERR {e!r}')

c.close()
print('closed OK')
