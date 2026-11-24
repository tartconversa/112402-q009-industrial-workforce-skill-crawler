# 制造岗位技能需求采集与培训席位联动

合成制造企业招聘、课程目录和推荐报名演练资料，岗位、课程及个人均用虚构代号。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `jobs.csv` contains 招聘来源版本、技能描述和当前状态。 Columns are `job`, `company`, `source`, `title`, `skills`, `location`, `updated`, `state`.
- `courses.csv` contains 课程目录覆盖范围、容量和开班期。 Columns are `course`, `provider`, `equipment`, `skills`, `seats`, `start`, `state`.
- `referrals.csv` contains 顾问推荐、席位预留及报名确认。 Columns are `referral`, `job`, `course`, `person`, `state`, `updated`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
