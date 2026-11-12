.PHONY: test run once install uninstall restart status logs

test:
	uv run pytest -q

run:            ## run in the foreground (stop the launchd one first: make uninstall)
	uv run python -m pr_radar

once:           ## poll once and print counts + health
	uv run python -m pr_radar --once

install:        ## launchd login item + SwiftBar plugin
	./scripts/install.sh

uninstall:
	./scripts/uninstall.sh

restart:        ## pick up config.toml edits
	launchctl kickstart -k gui/$$(id -u)/com.pr-radar

status:         ## health + resource usage
	@curl -s localhost:7777/api/health | python3 -m json.tool
	@ps -o pid,rss,%cpu,etime,command -p $$(lsof -ti tcp:7777 -sTCP:LISTEN) 2>/dev/null || echo "not running"

logs:
	tail -f ~/Library/Logs/pr-radar.log
