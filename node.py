import pandas as pd
from collections import defaultdict
import os
import ast
import hashlib
import json
import re
import torch
import comfy

_tag_cache = {}


def load_defaults_from_json():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(current_dir, "defaults_config.json")

    fallback_mapping = "{}"
    fallback_order = "[]"

    if not os.path.exists(config_path):
        print(f"[DanbooruTagSorter] Warning喵：未找到配置文件{config_path}喵，将使用空默认值喵。")
        return fallback_mapping, fallback_order

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        order_list = data.get("order", [])
        default_order_text = json.dumps(order_list, ensure_ascii=False)

        mapping_list = data.get("mapping", [])
        mapping_lines = []

        for i in mapping_list:
            if len(i) >= 3:
                cat, sub, target = i[0], i[1], i[2]
                # 复刻PythonDict的字符串行
                line = f'    ("{cat}", "{sub}"): "{target}"'  # 加个tab
                mapping_lines.append(line)

        # 搞半天要自己拼.jpg
        default_mapping_text = "{\n" + ",\n".join(mapping_lines) + "\n}"

        print(f"Sorter成功加载配置文件喵: {config_path}")
        return default_mapping_text, default_order_text

    except Exception as e:
        print(f"Sorter读取配置文件失败喵，请检查defaults_config.json路径及语法是否正确喵: {e}")
        return fallback_mapping, fallback_order


# 节点加载时先运行一次初始化默认值
DEFAULT_MAPPING_TEXT, DEFAULT_ORDER_TEXT = load_defaults_from_json()


# Sorter类
class DanbooruTagSorter:
    def __init__(self, excel_path, category_mapping, new_category_order, default_category="未归类词"):
        self.excel_path = excel_path
        self.category_mapping = category_mapping  # 映射规则 {('原有大类', '原有小类'): '新分类名'}
        self.new_category_order = new_category_order  # 定义输出时各个分类及各个分类的顺序
        self.default_category = default_category
        self.tag_db = self._load_database_with_cache()  # 初始化立刻先尝试加载或从缓存获取数据库

    # 根据原始的大类小类查表，得到新的分类名
    def get_new_category(self, original_category, original_subcategory):
        key = (original_category, original_subcategory)
        # 如果查不到就返回default_category，由用户自己设定
        return self.category_mapping.get(key, self.default_category)

    # 生成哈希键
    # 判断当前的配置参数是否和上次缓存一致
    def _generate_cache_key(self):
        params = {
            "excel_path": self.excel_path,
            # 将字典排序后dump为string，这样即使字典的key顺序不同，生成的哈希也一致
            "category_mapping": json.dumps(sorted(self.category_mapping.items())),
            "new_category_order": json.dumps(self.new_category_order),
            "default_category": self.default_category
        }
        params_str = json.dumps(params, sort_keys=True)
        hasher = hashlib.md5(params_str.encode(encoding='utf-8')).hexdigest()
        # 返回MD5
        return hasher

    # 加载数据库
    def _load_database_with_cache(self):
        cache_key = self._generate_cache_key()
        # 检查缓存是否命中
        if cache_key in _tag_cache:
            print(f"从缓存加载数据库喵:{self.excel_path}")
            return _tag_cache[cache_key]
        print(f"正在读取数据库喵:{self.excel_path} ...")  # 如果缓存未命中，则读取数据库

        # 基础校验
        if not self.excel_path or not os.path.exists(self.excel_path):
            print(f"警告喵：找不到文件或路径为空喵 {self.excel_path}")
            return {}

        try:
            #读取csv或者excel文件
            if self.excel_path.endswith('.csv'):
                df = pd.read_csv(self.excel_path)
            else:
                df = pd.read_excel(self.excel_path)

            tag_db = {}
            #遍历每一行，构建哈希表查询
            for index, row in df.iterrows():
                #清洗，转小写、去空格
                eng_tag = str(row['english']).strip().lower()
                cat = str(row['category']).strip()
                sub = str(row['subcategory']).strip()

                #计算该tag映射后是谁家的兵
                new_cat = self.get_new_category(cat, sub)
                #所有的下划线都替换为空格以匹配输入习惯
                clean_key = eng_tag.replace('_', ' ')
                tag_db[clean_key] = {
                    'original': eng_tag,
                    'original_category': cat,
                    'original_subcategory': sub,
                    'new_category': new_cat,
                    'rank': index
                }
            print(f"数据库加载完成喵，共索引{len(tag_db)}个 Tags喵。")

            # 存入全局缓存dict
            _tag_cache[cache_key] = tag_db
            return tag_db
        except Exception as e:
            print(f"读取数据库文件失败喵，请检查路径是否填写正确喵: {e}")
            return {}

    # 处理输入的Prompt字符串
    def process_tags(self, raw_string, add_category_comment=True,
                     regex_blacklist="", tag_blacklist="",
                     deduplicate=False):
        # 拆分输入字符串转列表
        input_tags = [t.strip() for t in raw_string.split(',') if t.strip()]

        # 去重
        if deduplicate and input_tags:
            seen = set()
            unique_tags = []
            for tag in input_tags:
                tag_lower = tag.lower()
                if tag_lower not in seen:
                    seen.add(tag_lower)
                    unique_tags.append(tag)
            input_tags = unique_tags

        # 精确匹配黑名单
        exact_blacklist_set = set()
        if tag_blacklist:
            exact_blacklist_set = {t.strip().lower() for t in tag_blacklist.split(',') if t.strip()}

        # 正则匹配黑名单
        regex_pattern = None
        if regex_blacklist:
            try:
                regex_pattern = re.compile(regex_blacklist, re.IGNORECASE)
            except re.error as e:
                print(f"正则表达式写错了喵:{e}")
        #初始化分类桶
        new_category_buckets = defaultdict(list)
        unmatched_tags = []

        allowed_categories_set = set(self.new_category_order)
        # 遍历每一个输入tag进行匹配
        for tag in input_tags:
            tag_clean = tag.strip()
            tag_lower = tag_clean.lower()
            # 黑名单check
            if (tag_lower in exact_blacklist_set or
                    (regex_pattern and regex_pattern.search(tag_clean))):
                continue
            lookup_key = tag_lower.replace('_', ' ')  # 构造查询Key
            if lookup_key in self.tag_db:  # 缓存命中
                info = self.tag_db[lookup_key]
                group_key = info['new_category']
                # 检查该分类是否在Order列表中
                if group_key in allowed_categories_set:
                    # 如果在Order里就正常归类
                    new_category_buckets[group_key].append((info['rank'], tag))
                else:
                    # 如果mapping有这个分类，但order里被删除了，视为未匹配，归入Default
                    unmatched_tags.append(tag)
            else:
                # 缓存未命中就丢到未匹配列表
                unmatched_tags.append(tag)

        #构建输出
        #categorized_tags给Getter节点用
        categorized_tags = {}
        for category in self.new_category_order:
            categorized_tags[category] = ""
        final_lines = []

        #将列表转为"tag1, tag2, "格式
        def format_tag_list(tag_list):
            if not tag_list:
                return ""
            else:
                return ", ".join(tag_list) + ", "

        # 按照用户定义的顺序new_category_order组装
        for category in self.new_category_order:
            if category in new_category_buckets:
                # 组内排序，根据数据库中的rank排序
                items = sorted(new_category_buckets[category], key=lambda x: x[0])
                current_tags_list = [item[1] for item in items]
                tags_str = format_tag_list(current_tags_list)
                categorized_tags[category] = tags_str  # 存入dict
                # 拼接最终
                if add_category_comment:
                    final_lines.append(f"{category}:")  # 添加 "新分类名:" 注释
                final_lines.append(tags_str)
                # 处理完后从桶中删除，后续可以处理剩余分类
                del new_category_buckets[category]
        # 上面的循环保证只有order中的Key会进桶，不需要再把order之外的Key追加到末尾了
        # 处理完全未匹配的Tags (包含数据库没找到的，以及被从Order里踢出去的)
        if unmatched_tags:
            unmatched_str = format_tag_list(unmatched_tags)
            target_unk = self.default_category
            if target_unk not in categorized_tags:
                categorized_tags[target_unk] = ""
            categorized_tags[target_unk] += unmatched_str  #追加到默认
            if add_category_comment:
                final_lines.append(f"{target_unk}:")
            final_lines.append(unmatched_str)
        return "\n".join(final_lines), categorized_tags


# ComfyUI
class DanbooruTagSorterNode:
    @classmethod
    def INPUT_TYPES(cls):
        # 输入节点：tags文本框、excel路径、两个配置文本框Mapping/Order
        return {
            "required": {
                "tags": ("STRING", {"multiline": True, "default": "", "placeholder": "1girl, solo..."}),
            },
            "optional": {
                "excel_file": ("STRING", {"multiline": False, "default": "danbooru_tags.xlsx"}),
                "category_mapping": ("STRING", {
                    "multiline": True,
                    "default": DEFAULT_MAPPING_TEXT,  # 加载自同目录json
                    "placeholder": "这里请输入小类映射到新分类的字典喵...注意语法正确喵..."
                }),
                "new_category_order": ("STRING", {
                    "multiline": True,
                    "default": DEFAULT_ORDER_TEXT,  # 加载自json
                    "placeholder": "这里请输入新分类以及输出顺序喵...注意语法正确喵..."
                }),
                "default_category": ("STRING", {"default": "未归类词"}),
                "regex_blacklist": ("STRING", {"default": ""}),
                "tag_blacklist": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "这里输入不想输出的tag喵...基础语法是 “tag1, tag2,” 喵..."}),
                "deduplicate_tags": ("BOOLEAN", {"default": False, "label": "自动去重"}),
                "validation": ("BOOLEAN", {"default": True, "label": "配置校验"}),
                "force_reload": ("BOOLEAN", {"default": False, "label": "强制重载"}),
                "is_comment": ("BOOLEAN", {"default": True, "label": "保留注释"}),
            }
        }

    RETURN_TYPES = ("TAG_BUNDLE", "STRING")
    RETURN_NAMES = ("分类数据包", "ALL_TAGS")
    FUNCTION = "process"
    CATEGORY = "Danbooru Tags"

    def process(self, tags, excel_file="danbooru_tags.xlsx", category_mapping="", new_category_order="",
                default_category="未归类词", regex_blacklist="", tag_blacklist="",
                deduplicate_tags=False, validation=True, force_reload=False, is_comment=True):

        # 自动定位
        current_DIR = os.path.dirname(os.path.abspath(__file__))
        data_base_dir = os.path.join(current_DIR, "tags_database")
        # 判断绝对/相对
        if os.path.isabs(excel_file) and os.path.exists(excel_file):
            final_excel_path = excel_file
        else:
            final_excel_path = os.path.join(data_base_dir, excel_file)  # 不是就返回相对

        def parse_input_data(raw_input, default_text, expected_type):
            # 如果输入已经是预期的对象Dict/List就直接返回
            if isinstance(raw_input, expected_type):
                return raw_input
            # 如果是其他非字符串对象就返回到默认文本
            if not isinstance(raw_input, str):
                raw_input = default_text
            # 此时确认为字符串，去除首尾空格
            text = raw_input.strip()
            if not text:
                text = default_text
            # 尝试解析
            try:
                return json.loads(text)
            except:
                try:
                    val = ast.literal_eval(text)
                    if isinstance(val, expected_type):
                        return val
                    raise ValueError(f"类型不匹配喵，想要这个喵：{expected_type}")
                except:
                    # 解析完全失败，尝试解析默认值作为保底
                    try:
                        return ast.literal_eval(default_text)
                    except:
                        # 默认值都挂了就返回空结构
                        return {} if expected_type is dict else []

        # 解析
        try:
            cat_map = parse_input_data(category_mapping, DEFAULT_MAPPING_TEXT, dict)
        except Exception as e:
            print(f"Mapping解析错误喵...{e}")
            cat_map = {}
        try:
            cat_order = parse_input_data(new_category_order, DEFAULT_ORDER_TEXT, list)
        except Exception as e:
            print(f"Order解析错误喵...{e}")
            cat_order = []

        # 校验Mapping和Order是否都有
        if validation:
            used = set(cat_map.values())
            defined = set(cat_order)
            missing = used - defined
            if missing:
                # raise中断执行并且提示用户
                raise ValueError(f"\n[配置错误喵]Mapping中使用了未在Order中定义的分类: {list(missing)}")

        # 运行逻辑
        if force_reload:
            global _tag_cache
            _tag_cache.clear()

        # 将处理好的绝对路径传递给Sorter
        sorter = DanbooruTagSorter(final_excel_path, cat_map, cat_order, default_category)
        all_str, cat_dict = sorter.process_tags(tags, is_comment, regex_blacklist, tag_blacklist, deduplicate_tags)
        return (cat_dict, all_str)


# Getter
class DanbooruTagGetterNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tag_bundle": ("TAG_BUNDLE",),
                "category_name": ("STRING", {"default": "角色特征词", "multiline": False}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("Tag String",)
    FUNCTION = "get_tag"
    CATEGORY = "Danbooru Tags"

    def get_tag(self, tag_bundle, category_name):
        # 防止空输入崩溃
        if not tag_bundle or not isinstance(tag_bundle, dict):
            return ("",)
        return (tag_bundle.get(category_name.strip(), ""),)


# 手动清除缓存
class DanbooruTagClearCacheNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {}}

    RETURN_TYPES = ()
    FUNCTION = "clear_cache"
    CATEGORY = "Danbooru Tags"
    OUTPUT_NODE = True

    def clear_cache(self):
        global _tag_cache
        _tag_cache.clear()
        print("缓存已经清除了喵...")
        return ()


# Registration 我的回合！注册！
NODE_CLASS_MAPPINGS = {
    "DanbooruTagSorterNode": DanbooruTagSorterNode,
    "DanbooruTagGetterNode": DanbooruTagGetterNode,
    "DanbooruTagClearCacheNode": DanbooruTagClearCacheNode
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DanbooruTagSorterNode": "Danbooru Tag Sorter (Packer)",
    "DanbooruTagGetterNode": "Danbooru Tag Getter (Extractor)",
    "DanbooruTagClearCacheNode": "Danbooru Tag Clear Cache"
}

# 都看到这里了球球给我点点Star吧...(哭
