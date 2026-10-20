.PHONY: clean pre-commit

clean:
	rm -rf build daily_checkin.egg-info dist

pre-commit:
	pre-commit run --all-files
