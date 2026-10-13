"""Versioned, server-owned operator procedures. Definitions are frozen in every run."""
from copy import deepcopy

def field(name, label, kind='text', required=True, **kw):
    return dict(name=name, label=label, type=kind, required=required, **kw)
def confirm(label='我已完成上述操作并核对目标模组'):
    return field('confirmed', label, 'boolean', must_be_true=True)
def step(id, title, instruction, operation=None, fields=None, records=None, risk=None):
    return dict(id=id, title=title, instruction=instruction,
                kind='automatic' if operation else 'manual', operation=operation,
                fields=fields or [], records=records or [], risk=risk)
def scenario(code, name, description, steps):
    return dict(code=code, name=name, version='1.0.0', description=description, steps=steps)

INSPECT=step('inspect','读取并锁定当前模组','保持目标模组连接在当前 USB 端口。点击开始后读取 CPU 序列号、SN、固件版本和健康状态。','inspect',records=['CPU serial / USB 端口','原始 SN 与版本','命令与健康基线'])
HEALTH=step('verify_health','检查模组恢复情况','确认目标模组已恢复连接，读取身份、SN、版本和健康状态。','verify_health',records=['恢复后的身份与固件版本','健康检查原始输出'])
DEFINITIONS=[
scenario('SPECIAL-SN-POWER-CYCLE','手动断电后 SN 持久化','写入指定 SN 后立即人工断电，确认真实掉线，再插回同一端口核验。测试 SN 保留，原 SN 留档。',[
    INSPECT,
    step('set_sn','写入测试 SN','填写测试 SN。确认后执行写入；写入完成即进入拔线步骤，请立即拔下模组供电/USB，避免等待额外操作。','set_sn',
         [field('target_sn','待写入 SN',pattern=r'^[A-Za-z0-9_-]{1,56}$'),confirm('确认写入此 SN，写入成功后立即断电')],['原 SN','目标 SN','写入命令完成时间'], '修改模组 SN'),
    step('verify_disconnected','立即拔下模组并确认断电','立即拔下模组供电及 USB。完成后确认；系统验证锁定物理端口已经消失，保留掉线证据。','verify_disconnected',
         [confirm('模组已经断电并拔下')],['操作员确认时间','写入至确认的时间','USB 端口消失证据']),
    step('verify_sn','重新插上并核对 SN','把同一模组插回原 USB 端口，等待模组启动后确认。系统比对 CPU serial、端口和刚设置的 SN。','verify_sn',
         [confirm('同一模组已插回原端口并启动')],['重连身份','实际 SN / 预期 SN','一致性判定']),
    step('finish','完成并保留测试 SN','已记录原始 SN 和测试 SN。本流程保留测试 SN；如需恢复，请在测试结束后另行操作并保留记录。',fields=[confirm('确认已知本次测试 SN 将保留')],records=['操作员收尾确认'])
]),
scenario('SPECIAL-LOG-FULL','日志区增长与 rolling 异常','累计写入超过 20 MiB 的受控日志文件，核验日志回收及模组健康。累计字节数不等同实际满盘。',[
    INSPECT,
    step('configure','确认日志压力参数','仅向模组日志测试路径写入受控文件。单文件不超过 60000 字节；操作会消耗闪存写入和日志空间。没有 rolling 或边界证据时不能通过。',
         fields=[field('total_mib','累计传输 MiB','number',default=24,min=21,max=64),confirm('确认在可恢复的测试模组上执行日志压力')],records=['传输目标字节数','测试文件命名范围'],risk='持续向模组日志区域写入文件'),
    step('log_pressure','执行累计日志写入','开始后持续写入受控日志文件，并记录每次成功字节数、目录快照和命令输出。保持模组连接。','log_pressure',records=['实际成功传输字节数','写入失败详情','目录快照']),
    step('log_verify','验证 rolling 与边界证据','读取日志目录变化，检查已写入文件是否被 rolling 回收，并检查设备状态。未观测到边界/回收证据将标记阻塞。','log_verify',records=['rolling 删除/回收证据','目录占用','是否观测到实际满盘']),
    HEALTH
]),
scenario('FACTORY-AGING','老化座与串口监测','先准备采集，再由操作员接入老化座供电。支持 3588 USB 串口与人工提交原始日志，两种路径使用相同阈值。',[
    INSPECT,
    step('serial_probe','检查 3588 串口能力','将串口适配器接至 3588，串口线接模组。此时先不要启动老化。系统枚举 USB 串口和权限；无可用串口可选择人工采集。','serial_probe',records=['USB serial / by-id 路径','访问权限']),
    step('configure','设置老化条件','Hub 判定时长不修改固件 /data/aging_time，固件默认 14400 秒。请先确认固件周期；旧 aging_result 必须由操作员明确执行 set-ft-aging -2 重置并记录。填写老化周期和温度上限。串口模式选择检测到的设备路径；人工模式稍后粘贴完整原始日志。确认后下一步启动采集。',
         fields=[field('capture_mode','采集方式','select',default='serial',options=[{'value':'serial','label':'3588 USB 串口'},{'value':'manual','label':'人工采集原始日志'}]),
                 field('serial_device','USB 串口路径',required=False,help='/dev/ttyUSB0、/dev/ttyACM0 或 /dev/serial/by-id/...'),
                 field('baud_rate','波特率','number',default=115200,min=1200,max=4000000),
                 field('duration_seconds','要求老化时长（秒）','number',default=14400,min=60,max=86400),
                 field('capture_timeout_seconds','采集最长等待（秒）','number',default=15000,min=660,max=90000),
                 field('aging_reset_command','本轮重置旧老化结果的实际命令/操作','textarea'),field('max_temperature_c','最高允许温度 °C','number',min=1,max=150),confirm('确认串口接线、电平和老化座已准备好')],records=['采集方式','时长与温度判定阈值']),
    step('aging_capture','开始采集后给老化座上电','串口模式点击开始后，必须等待界面显示“串口采集已就绪”，再给老化座上电。人工模式先在本地串口工具启动完整原始日志保存，再给老化座上电。','aging_capture',records=['采集开始与结束时间','本轮老化日志','串口原始数据']),
    step('aging_verify','提交证据并判定老化','人工模式粘贴本轮完整原始日志，并填写 ISO 时间（含时区，例如 2026-10-08T10:00:00+08:00）。串口模式使用自动采集记录，可不填写人工字段。必须包含本轮开始、遥测和结束证据。','aging_verify',
         [field('manual_log_artifact_id','上传人工采集原始日志（最大 64 MiB）','file',required=False,help='与粘贴日志二选一，完整原始日志保留 SHA-256'),field('manual_log','人工采集原始日志','textarea',required=False,help='粘贴最多 500000 字符；更大日志请上传文件'),field('started_at','人工采集开始时间',required=False),field('ended_at','人工采集结束时间',required=False),confirm('已完成本轮老化并提交本轮证据')],['实际持续时间','温度峰值','错误/重启记录','最终老化结果'])
]),
scenario('SPECIAL-OTA','OTA 异常与恢复','每轮选择一种异常：损坏包、错误包名或升级中断。服务器准备测试副本，操作员按门控执行真实 OTA 注入，系统核验原始日志并回检设备。',[
    INSPECT,
    step('configure','选择异常并锁定源包','源包必须已经位于 3588，填写绝对路径和完整 SHA-256。系统复制生成测试包并保留原始包。每轮只测一种异常；另外两种需分别新建。',
         fields=[field('variant','异常类型','select',default='corrupt',options=[{'value':'corrupt','label':'损坏 OTA 包'},{'value':'wrong_name','label':'错误 OTA 包名'},{'value':'interrupted','label':'OTA 进行中断电'}]),
                 field('package_path','3588 上源 OTA 包绝对路径'),field('expected_sha256','源包 SHA-256',pattern=r'^[0-9a-fA-F]{64}$'),
                 confirm('确认测试包适配当前模组且具备人工恢复条件')],records=['异常类型','源包摘要','包路径'],risk='异常 OTA 可能需要人工恢复'),
    step('ota_prepare','生成并核验异常测试包','校验源包 SHA-256，生成本轮独立副本，并给出需要人工执行的传输/触发说明。此步不会自动启动 OTA。','ota_prepare',records=['原包与异常包 SHA-256','目标文件名与 MD5','人工执行命令']),
    step('ota_start','人工执行 OTA 注入','按照上一步结果给出的操作说明，将测试包送到锁定模组并触发升级。记录实际执行的完整命令。中断场景要观察到升级开始日志后拔掉模组供电。',
         fields=[field('command','实际执行命令与操作','textarea'),confirm('已按本轮异常方案触发 OTA 并保存完整升级日志')],records=['实际执行命令','操作员与触发时间'],risk='人工触发 OTA / 中断电源'),
    step('ota_status','恢复供电并提交升级证据','中断场景记录升级开始/进度和断电时间，再恢复供电。所有场景插回原 USB 端口后提交完整 OTA 原始日志；系统判定异常是否正确拒绝，同时回检模组身份与健康。','ota_status',
         [field('ota_log','完整 OTA 原始日志','textarea'),field('progress_before_cut','断电前升级阶段/进度','textarea',required=False),field('cut_at','断电时间 ISO（含时区）',required=False),confirm('已恢复供电并将同一模组接回原端口')],['拒绝或升级行为','断电时刻与阶段','恢复身份 / 版本 / 健康']),
    HEALTH
])]
def definitions():
    return deepcopy(DEFINITIONS)
def definition(code):
    return next((deepcopy(item) for item in DEFINITIONS if item['code']==code),None)
