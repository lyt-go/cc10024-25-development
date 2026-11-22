"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import manifest, resource

# manifest 的固定路径要排在资源路由之前，避免 /api/{module}/{entry_id} 抢先匹配。
ROUTERS = [manifest, resource]
