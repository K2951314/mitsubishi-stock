import time
import re
import os
import subprocess
import configparser
from playwright.sync_api import sync_playwright

# ==================== 配置文件读取 ====================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


def parse_query_line(line):
    """
    解析单条查询输入，返回 (model, material, display_prefix)

    解析逻辑（位置推断，不依赖材质字符模式）：
    1. 管道分隔：CNMG120408-MA | MP7135 → 直接拆分
    2. 完整行：先掐头（料号，数字开头）去尾（含税价格），
       中间剩余的第一个 token 为型号，第二个为材质。
       如 "01.02.00564 SEEN1203AFTN1 NX2525 含税27.6"
       → 掐头去尾后 "SEEN1203AFTN1 NX2525" → 型号=SEEN1203AFTN1 材质=NX2525
    """
    if "|" in line:
        parts = [p.strip() for p in line.split("|", 1)]
        model = parts[0]
        material = parts[1] if len(parts) > 1 else ""
        return model, material, None

    tokens = line.split()
    if not tokens:
        return "", "", None

    if len(tokens) == 1:
        return tokens[0], "", None

    # --- 第一步：掐头（跳过料号）---
    # 料号特征：以数字开头，如 01.02.00564
    start = 0
    if re.match(r'^\d', tokens[0]):
        start = 1

    # --- 第二步：去尾（找到"含税"价格的位置）---
    end = len(tokens)
    for i in range(len(tokens) - 1, start - 1, -1):
        if "含税" in tokens[i]:
            end = i
            break

    # --- 第三步：中间部分即 型号 [材质] ---
    middle = tokens[start:end]
    model = middle[0] if len(middle) > 0 else ""
    material = middle[1] if len(middle) > 1 else ""

    # 有料号或含税价 → 保留原始行作为输出前缀
    has_prefix = (start > 0 or end < len(tokens))
    display_prefix = line if has_prefix else None

    return model, material, display_prefix


def load_config():
    """
    从 config.ini 读取账号密码和查询列表。
    返回 (username, password, query_list)
    query_list 每项为 (model, material, display_prefix)
    """
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ 未找到配置文件: {CONFIG_PATH}")
        print("请先创建 config.ini，格式参考：")
        print("  [account]")
        print("  username = YOUR_USERNAME")
        print("  password = YOUR_PASSWORD")
        print("  [query]")
        print("  queries =")
        print("      CNMG120408-MA | MP7135")
        print("      01.01.24680 CNMG120408-MA MP7135 含税28.5")
        return None, None, None

    cfg = configparser.RawConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")

    username = cfg.get("account", "username", fallback="").strip()
    password = cfg.get("account", "password", fallback="").strip()

    # 解析批量查询列表
    raw_queries = cfg.get("query", "queries", fallback="").strip()
    query_list = []
    for line in raw_queries.splitlines():
        line = line.strip()
        if not line:
            continue
        model, material, prefix = parse_query_line(line)
        if model:
            query_list.append((model, material, prefix))

    return username, password, query_list


# 💡 精准修正：使用前部固定的正数索引，避开尾部的操作按钮干扰
INDEX_SHANGHAI = 4    # 上海原库存在列表第 5 个位置（索引4）
INDEX_JAPAN = 5       # 日库库存在列表第 6 个位置（索引5）
INDEX_CDC = 6         # CDC库存在列表第 7 个位置（索引6）
# ==================================================


def clean_number(val_str):
    try:
        return int(re.sub(r'[^\d]', '', val_str))
    except ValueError:
        return 0


def copy_to_clipboard(text):
    """将文本复制到系统剪贴板（Windows）"""
    try:
        import base64
        # 将 PowerShell 脚本编码为 UTF-16LE → Base64，通过 -EncodedCommand 传入
        # 这样完全绕过 stdin 的编码问题（中文 Windows 默认 GBK 管道会导致乱码）
        ps_script = f"Set-Clipboard -Value '{text}'"
        encoded = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        subprocess.run(
            ['powershell', '-NoProfile', '-EncodedCommand', encoded],
            check=True, capture_output=True
        )
        print("📋 结果已复制到系统剪贴板！")
    except Exception as e:
        print(f"⚠️ 复制到剪贴板失败: {e}")


def do_query(page, model_val, material_val, display_prefix=None):
    """
    执行单条库存查询，返回格式化结果字符串。
    material_val 为空时仅按型号查询。
    display_prefix 为原始输入行时，拼在结果前面以便直接粘贴使用。
    """
    mat_label = material_val if material_val else '(空)'
    print(f"\n--- 开始查询: 型号={model_val}  材质={mat_label} ---")

    has_material = bool(material_val)

    # ------------------ 定位并填写查询条件 ------------------
    try:
        model_input = page.locator('xpath=(//*[contains(text(), "形状（ISO）")]/following::input)[1]')
        model_input.wait_for(state="visible", timeout=20000)

        model_input.fill("")
        model_input.fill(model_val)
        print(f"  -> '形状（ISO）'已填入: {model_val}")

        material_input = page.locator('xpath=(//*[contains(text(), "材质") and not(contains(text(), "形状"))]/following::input)[1]')
        if has_material:
            material_input.fill("")
            material_input.fill(material_val)
            print(f"  -> '材质'已填入: {material_val}")
        else:
            material_input.fill("")
            print("  -> '材质'已清空（仅型号查询）")

    except Exception as e:
        print(f"  ❌ 定位查询输入框失败！错误: {e}")
        return None

    # ------------------ 模拟回车检索 ------------------
    try:
        if has_material:
            material_input.press("Enter")
        else:
            model_input.press("Enter")
    except Exception as e:
        print(f"  模拟回车键失败: {e}")
        return None

    # ------------------ 等待结果 ------------------
    try:
        page.wait_for_selector('div[role="presentation"][cellclipdiv="true"]', timeout=20000)
        time.sleep(2)
    except Exception:
        print("  未能在规定时间内加载出结果。")
        return None

    # ------------------ 解析库存数据 ------------------
    try:
        # 收集所有匹配行，用于判断是否多结果
        if has_material:
            all_rows = page.locator('tr').filter(has_text=model_val).filter(has_text=material_val).all()
        else:
            all_rows = page.locator('tr').filter(has_text=model_val).all()

        # 提取每一行的第一个单元格（完整型号）用于去重判断
        row_models = []
        for row in all_rows:
            first_cells = row.locator('div[role="presentation"][cellclipdiv="true"] nobr').all_text_contents()
            if not first_cells:
                first_cells = row.locator('div[role="presentation"][cellclipdiv="true"]').all_text_contents()
            if first_cells:
                row_models.append(first_cells[0].strip())

        # 去重后的型号列表
        unique_models = list(dict.fromkeys(m for m in row_models if m and m != '\xa0'))
        print(f"  -> 匹配到 {len(unique_models)} 条结果行，型号列表: {unique_models}")

        # 构建输出前缀：display_prefix 优先（保留原始行），否则用 型号 + 材质
        if display_prefix:
            out_prefix = display_prefix
        else:
            out_prefix = f"{model_val} {material_val}" if has_material else model_val

        # 多条不同型号 → 型号不全
        if len(unique_models) > 1:
            models_str = ", ".join(unique_models)
            print(f"  ⚠️ 匹配到多条不同型号，输入型号可能不完整")
            return f"{out_prefix} 型号不全，匹配到多条: {models_str}"

        # 取第一条（也是唯一一条）匹配行
        target_row = all_rows[0] if all_rows else None
        if not target_row or not target_row.is_visible():
            print("  未找到可见的匹配行。")
            return None

        cells = target_row.locator('div[role="presentation"][cellclipdiv="true"] nobr').all_text_contents()
        if not cells:
            cells = target_row.locator('div[role="presentation"][cellclipdiv="true"]').all_text_contents()

        if not cells:
            print("  未能在行中找到有效的单元格内容。")
            return None

        print(f"  -> 提取到原始整行数据: {cells}")

        # 用结果表中的完整型号替代输入值（仅在没有 display_prefix 时使用）
        full_model = cells[0].strip() if cells[0].strip() and cells[0].strip() != '\xa0' else model_val

        # 自动补全材质：仅型号查询时，从结果行第2个单元格提取材质
        result_material = ""
        if not has_material and len(cells) > 1:
            mat_candidate = cells[1].strip()
            if mat_candidate and mat_candidate != '\xa0':
                result_material = mat_candidate

        shanghai_raw = cells[INDEX_SHANGHAI]
        japan_raw = cells[INDEX_JAPAN]
        cdc_raw = cells[INDEX_CDC]

        shanghai_qty = clean_number(shanghai_raw)
        japan_qty = clean_number(japan_raw)
        cdc_qty = clean_number(cdc_raw)

        # 合并 CDC 到上海库存
        final_shanghai_qty = cdc_qty + shanghai_qty
        final_japan_qty = japan_qty

        # 构建库存部分：只返回不为 0 的库存
        inventory_parts = []
        if final_shanghai_qty > 0:
            inventory_parts.append(f"上海库存{final_shanghai_qty}")
        if final_japan_qty > 0:
            inventory_parts.append(f"日本库存{final_japan_qty}")

        if not inventory_parts:
            inventory_str = "无货"
        else:
            inventory_str = " ".join(inventory_parts)

        # 拼接最终结果
        if display_prefix:
            # 完整行模式：原始行 + 库存（如 "01.01.24680 CNMG120408-MA MP7135 含税28.5上海库存502 日本库存1920"）
            labelled_result = f"{out_prefix}{inventory_str}"
        else:
            # 简单模式：用完整型号 + 材质 + 库存
            # 材质来源：用户指定 > 结果行自动补全 > 无
            show_material = material_val if has_material else result_material
            label = f"{full_model} {show_material}" if show_material else full_model
            labelled_result = f"{label}{inventory_str}"

        return labelled_result

    except Exception as e:
        print(f"  解析库存数据发生错误: {e}")
        return None


def run():
    username, password, query_list = load_config()
    if username is None:
        return

    if not query_list:
        print("❌ 配置文件中未找到有效的查询条目，请检查 config.ini 的 [query] queries 部分。")
        return

    print(f"📋 本次共 {len(query_list)} 条查询任务：")
    for i, (m, mat, prefix) in enumerate(query_list, 1):
        prefix_info = f"  (行: {prefix})" if prefix else ""
        print(f"   {i}. 型号={m}  材质={mat or '(空)'}{prefix_info}")

    with sync_playwright() as p:
        print("\n正在尝试连接到您日常使用的 Edge 浏览器 (127.0.0.1:9222)...")
        try:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
        except Exception:
            print(f"\n❌ 连接失败！（连接超时）")
            print("="*22 + " 🛠️ 端口排查与解决步骤 " + "="*22)
            print("原因：您的 Edge 浏览器当前没有真正开启 9222 调试端口。")
            print("\n请按以下 3 步排查并重新开启端口：")
            print("1. 【验证端口】：在 Edge 浏览器地址栏输入并访问： http://127.0.0.1:9222/json/version")
            print('   - 如果网页显示"无法访问此网站"，说明端口确实没开，请继续第 2 步。')
            print("   - 如果能看到一堆英文代码，说明端口是开着的，请直接再次尝试运行脚本。")
            print("\n2. 【彻底杀干净 Edge】：")
            print("   - 关闭所有看得见的 Edge 窗口。")
            print('   - 按 Ctrl+Shift+Esc 打开"任务管理器"，在"进程"列表中找到所有名为 \'Microsoft Edge\' 的进程，右键选中它们并点击"结束任务"。')
            print("\n3. 【重新开启调试版 Edge】：")
            print("   - 按下键盘上的 Win + R 键打开运行窗口，输入 cmd 回车。")
            print("   - 复制并运行以下命令：")
            print('     start msedge.exe --remote-debugging-port=9222')
            print("\n4. 在打开的 Edge 浏览器中再次访问 http://127.0.0.1:9222/json/version，确认能看到代码后，再重新运行此 Python 脚本。")
            print("="*66 + "\n")
            return

        print("🎉 成功连接 Edge 浏览器！")
        context = browser.contexts[0]
        page = context.new_page()

        # ------------------ 智能检测 ------------------
        is_already_on_query_page = False
        try:
            model_input = page.locator('xpath=(//*[contains(text(), "形状（ISO）")]/following::input)[1]')
            if model_input.is_visible():
                is_already_on_query_page = True
                print("🎉 检测到当前标签页已处于查询界面，直接开始查询！")
        except Exception:
            pass

        # ------------------ 菜单流点击导航 ------------------
        if not is_already_on_query_page:
            print("1. 正在访问主页...")
            try:
                page.goto("https://mcweb.mitsubishi-materials.com/concerto-mmsc-ec/login.jsp", wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
            except Exception as e:
                print(f"打开主页超时: {e}")
                page.close()
                return

            # 判断是否需要登录
            if "login.jsp" in page.url or page.locator('select#language').is_visible():
                print("🔔 检测到未登录，开始自动登录...")
                try:
                    page.wait_for_selector('select#language option[value="zh_CN"]', state="attached", timeout=15000)
                    page.select_option('select#language', value='zh_CN')
                    time.sleep(1.5)

                    username_input = page.locator('tr:has-text("Account Id") input, tr:has-text("Account ID") input, input[name="userId"]').first
                    username_input.fill(username)

                    password_input = page.locator('tr:has-text("Password") input, input[name="password"]').first
                    password_input.fill(password)

                    page.locator('img[alt="sign in"]').click()
                    print("已点击登录，等待页面加载...")
                    time.sleep(5)
                except Exception as log_err:
                    print(f"自动登录过程中出错: {log_err}")
                    page.close()
                    return
            else:
                print("🎉 浏览器中检测到已登录状态，跳过登录步骤！")

            # 模拟点击导航菜单进入查询页
            print("3. 开始点击菜单导航...")
            try:
                menu_btn = page.locator('td.projectToolStripMenuButtonTitle', has_text="订购・查库存").first
                menu_btn.wait_for(state="visible", timeout=20000)
                menu_btn.click()
                print("-> 已点击：'订购・查库存'")
                time.sleep(1.5)

                submenu_btn = page.locator('nobr', has_text="查库存并订购").first
                submenu_btn.wait_for(state="visible", timeout=15000)
                submenu_btn.click()
                print("-> 已点击：'查库存并订购'")
                time.sleep(4)
            except Exception as nav_err:
                print(f"❌ 模拟菜单点击导航失败！错误信息: {nav_err}")
                page.close()
                return

        # ==================== 批量查询循环 ====================
        all_results = []
        for idx, (model_val, material_val, display_prefix) in enumerate(query_list, 1):
            print(f"\n{'='*20} [{idx}/{len(query_list)}] {'='*20}")
            result = do_query(page, model_val, material_val, display_prefix)
            if result:
                all_results.append(result)
            else:
                # 查询失败时的格式化输出
                if display_prefix:
                    all_results.append(f"{display_prefix} 查询失败或无结果")
                else:
                    mat_tag = f" {material_val}" if material_val else ""
                    all_results.append(f"{model_val}{mat_tag} 查询失败或无结果")

        # ==================== 汇总输出 ====================
        print("\n" + "="*30 + " 批量查询结果汇总 " + "="*30)
        final_text = "\n".join(all_results)
        print(final_text)
        print("="*78 + "\n")

        # 复制到系统剪贴板
        if all_results:
            copy_to_clipboard(final_text)

        # 操作完毕关闭标签页
        page.close()
        print("新建的标签页已关闭。")


if __name__ == "__main__":
    run()
