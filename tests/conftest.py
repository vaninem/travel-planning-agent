"""普通回归测试不调用实际攻略嵌入服务；向量行为测试显式注入替身。"""

import os


os.environ["TRAVEL_GUIDE_RETRIEVAL"] = "lexical"
