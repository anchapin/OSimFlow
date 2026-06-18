# OpenStudio-Standards subclass for 179D

A specific subclass of ASHRAE 90.1-2007 subclass for 179D.

How to load it in your measure (we sort by size to make sure the base one (`179d_ashrae_90_1_2007.rb`) is loaded first

```ruby
require 'openstudio-standards'
require 'pathname'
(Pathname.new(__dir__) / 'resources/179d_standards').glob('*.rb').sort_by { |p| p.basename.to_s.size }.each { |f| require f.sub_ext('').to_s } unless defined?(ACM179dASHRAE9012007)

[...]

std = Standard.build('179D 90.1-2007')
```

* **Main branch**: https://github.com/NREL/openstudio-standards/tree/179D_310
* **Retrieved at commit**: [a440b42c746d81d34a082c7ecfb42c5ef57a88d8](https://github.com/NREL/openstudio-standards/commit/a440b42c746d81d34a082c7ecfb42c5ef57a88d8)
