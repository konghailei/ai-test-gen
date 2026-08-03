将预构建的前端镜像导出为 10-ai-test-gen-web.tar 放在此目录。
构建方式（在外网环境）：
cd frontend && docker build -t ai-test-gen-web:latest .
docker save ai-test-gen-web:latest -o ../images/10-ai-test-gen-web.tar
