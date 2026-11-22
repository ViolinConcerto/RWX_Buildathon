# Local loop. `make sample` mirrors the scorer's run flags exactly (see starter/Makefile)
# and also reports the container's CPU seconds, read from its cgroup at exit.
IMAGE ?= blast-radius-entry
SAMPLE ?= $(CURDIR)/starter/sample
OUTPUT := $(CURDIR)/work/output.jsonl
export PATH := $(CURDIR)/tools/bin:$(PATH)

RUN_FLAGS := --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample

build:
	docker build -q --platform linux/amd64 --tag $(IMAGE) "$$(wslpath -w . 2>/dev/null || pwd)"

sample: build
	@mkdir -p work
	tools/run_sample.sh "$(IMAGE)" "$(OUTPUT)" "$(SAMPLE)" $(RUN_FLAGS)
