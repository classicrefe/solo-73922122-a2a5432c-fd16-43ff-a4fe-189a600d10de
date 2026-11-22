# 使 tests/ 下的用例能 import app 包（pytest 会把本文件所在目录加入 sys.path）
#
# 收集测试前先指向临时库：app.main 在模块级创建默认 app（create_app()），
# 否则导入时会打开并升级仓库里的 data/elev.db，污染版本库中的样例库。
import os
import tempfile

os.environ["ELEV_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="elev-test-import-"), "import.db")
