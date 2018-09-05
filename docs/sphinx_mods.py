# Copyright (c) 2014 Evalf
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import inspect, pathlib, shutil, os, runpy, urllib.parse, shlex, doctest, re, io, hashlib, base64, treelog, html
import docutils.nodes, docutils.parsers.rst, docutils.statemachine
import sphinx.util.logging, sphinx.util.docutils, sphinx.addnodes
import nutils.matrix, nutils.testing
import numpy

project_root = pathlib.Path(__file__).parent.parent.resolve()

def process_signature(self, objtype, fullname, object, options, args, retann):
  if objtype in ('function', 'class', 'method'):
    try:
      signature = inspect.signature(object)
    except ValueError:
      # Some builtins have no signature.
      return
  else:
    return
  # Drop annotations from signature.
  signature = signature.replace(parameters=(param.replace(annotation=param.empty) for param in signature.parameters.values()),
                                return_annotation=inspect.Signature.empty)
  # Return a string representation of args and of the return annotation.  Note
  # that `str(signature)` would have included the return annotation if we
  # hadn't removed it above.
  return str(signature).replace('\\', '\\\\'), ''

def print_rst_autogen_header(*, file, src=None):
  print('..', file=file)
  print('   Automatically generated.  Edits are futile.', file=file)
  print(file=file)
  print(':autogenerated:', file=file)
  if src is not None:
    abssrc = src.resolve().relative_to(project_root)
    print(':autogeneratedfrom: {}'.format(abssrc), file=file)
  print(file=file)

def print_rst_h1(text, *, file):
  assert '\n' not in text
  print(file=file)
  print(text, file=file)
  print('='*len(text), file=file)
  print(file=file)

def print_rst_label(name, *, file):
  print(file=file)
  print('.. _{}:'.format(name), file=file)
  print(file=file)

def copy_utime(src, dst):
  stat = os.stat(str(src))
  os.utime(str(dst), ns=(stat.st_atime_ns, stat.st_mtime_ns))

def generate_examples(app):
  dst_examples = pathlib.Path(app.srcdir)/'examples'
  dst_examples.mkdir(parents=True, exist_ok=True)

  srcs = tuple(f for f in sorted(project_root.glob('examples/*.py')) if f.name != '__init__.py')
  for src in sphinx.util.status_iterator(srcs, 'generating examples... ', 'purple', len(srcs), app.verbosity):
    name = src.name
    dst = dst_examples/(src.with_suffix('.rst').name)

    with dst.open('w') as f_dst:
      print_rst_autogen_header(file=f_dst, src=src)
      # Add a label such that you can reference an example by
      # :ref:`examples/laplace.py`.
      print_rst_label('examples/{}'.format(name), file=f_dst)
      print_rst_h1(name, file=f_dst)
      print('.. exampledoc:: {}'.format(src.relative_to(project_root).as_posix()), file=f_dst)
    copy_utime(src, dst)

class LineIter:

  def __init__(self, lines):
    self._lines = iter(lines)
    self._index = -1
    self._next = None
    self.__next__()

  def __bool__(self):
    return self._next != StopIteration

  def __iter__(self):
    return self

  def __next__(self):
    if self._next == StopIteration:
      raise StopIteration
    value = self._index, self._next
    try:
      self._next = next(self._lines)
      self._index += 1
    except StopIteration:
      self._next = StopIteration
    return value

  @property
  def peek(self):
    if self._next == StopIteration:
      raise ValueError
    else:
      return self._next

class ExampleDocDirective(docutils.parsers.rst.Directive):

  has_content = False
  required_arguments = 1
  options_arguments = 0

  @staticmethod
  def _isdocline(line):
    line = line.lstrip()
    return line.rstrip() == '#' or line.startswith('# ')

  def run(self):
    logger = sphinx.util.logging.getLogger(__name__)
    nodes = []

    src = project_root/self.arguments[0]
    with src.open('r') as f:
      prevtype = None
      lines = LineIter(f)
      if lines and lines.peek.startswith('#!'):
        next(lines)
      while lines:
        if lines.peek.rstrip('\n') == '':
          next(lines)
        elif self._isdocline(lines.peek):
          # Collect all doc lines.
          contents = docutils.statemachine.ViewList()
          while lines and self._isdocline(lines.peek):
            i, line = next(lines)
            contents.append(line.lstrip()[2:], self.arguments[0], i)
          # Parse as rst into `node`.
          with sphinx.util.docutils.switch_source_input(self.state, contents):
            node = docutils.nodes.container()
            self.state.nested_parse(contents, 0, node)
          # Process sh roles.  Add links to logs.
          for sh_node in node.traverse(docutils.nodes.literal):
            if 'nutils_sh' not in sh_node:
              continue
            cmdline = sh_node.get('nutils_sh')
            cmdline_parts = tuple(shlex.split(cmdline))
            if cmdline_parts[:2] != ('python3', src.name):
              logger.warn('Not creating a log for {}.'.format(cmdline))
              continue
            log_link = sphinx.addnodes.only(expr='html')
            log_link.append(docutils.nodes.inline('', ' '))
            xref = sphinx.addnodes.pending_xref('', reftype='nutils-log', refdomain='std', reftarget=cmdline_parts[2:], script=src)
            xref += docutils.nodes.inline('', '(view log)', classes=['nutils-log-link'])
            log_link += xref
            sh_node.parent.insert(sh_node.parent.index(sh_node)+1, log_link)
          nodes.extend(node.children)
        else:
          # Collect all source lines.
          istart, line = next(lines)
          contents = [line]
          while lines and not self._isdocline(lines.peek):
            i, line = next(lines)
            contents.append(line)
          # Remove trailing empty lines.
          while contents and contents[-1].rstrip('\n') == '':
            del contents[-1]
          contents = ''.join(contents)
          # Create literal block.
          literal = docutils.nodes.literal_block(contents, contents)
          literal['language'] = 'python3'
          literal['linenos'] = True
          literal['highlight_args'] = dict(linenostart=istart+1)
          sphinx.util.nodes.set_source_info(self, literal)
          nodes.append(literal)

    return nodes

def role_sh(name, rawtext, text, lineno, inliner, options={}, context=[]):
  return [docutils.nodes.literal('', text, nutils_sh=text)], []

def create_log(app, env, node, contnode):
  logger = sphinx.util.logging.getLogger(__name__)

  if node['reftype'] == 'nutils-log':
    script = node.get('script')
    scriptname = str(script.relative_to(project_root))

    cmdline_args = node['reftarget']
    cmdline = ' '.join(map(shlex.quote, [scriptname, *cmdline_args]))

    target = '_logs/{}/index'.format(urllib.parse.quote(cmdline, safe='').replace('%', '+'))

    dst_log = (pathlib.Path(app.builder.outdir)/target).parent
    if dst_log.exists() and dst_log.stat().st_mtime > script.stat().st_mtime:
      logger.debug('Skip building log of {cmdline} because it already exists and '
                   'is newer than {script}.  Please touch {script} to force a rebuild.'
                   .format(script=scriptname, cmdline=cmdline))
    else:
      if dst_log.exists():
        logger.debug('purging old log files... {}'.format(dst_log))
        shutil.rmtree(str(dst_log))
      else:
        dst_log.parent.mkdir(parents=True, exist_ok=True)
      logger.info('creating log... {}'.format(cmdline))
      script_dict = runpy.run_path(str(script), run_name='__log_builder__')
      # Parse cmdline.
      params = inspect.signature(script_dict['main']).parameters.values()
      kwargs = {param.name: param.default for param in params}
      for arg in cmdline_args:
        if not arg:
          continue
        name, sep, value = arg.lstrip('-').partition('=')
        if not sep:
          value = not name.startswith('no')
          if not value:
            name = name[2:]
        if name not in kwargs:
          logger.error('unkown argument {!r}'.format(name))
          return
        default = kwargs[name]
        try:
          if isinstance(default, bool) and not isinstance(value, bool):
            raise Exception('boolean value should be specifiec as --{0}/--no{0}'.format(name))
          kwargs[name] = default.__class__(value)
        except Exception as e:
          logger.error('invalid argument for {!r}: {}'.format(name, e))
          return
      # Run script.
      func = script_dict['main']
      with treelog.HtmlLog(str(dst_log), title=scriptname, htmltitle='{} {}'.format(nutils.cli.SVGLOGO, html.escape(scriptname)), favicon=nutils.cli.FAVICON) as log, treelog.set(log), nutils.matrix.backend('scipy'), nutils.warnings.via(treelog.warning):
        log.write('<ul style="list-style-position: inside; padding-left: 0px; margin-top: 0px;">{}</ul>'.format(''.join(
          '<li>{}={} <span style="color: gray;">{}</span></li>'.format(param.name, kwargs.get(param.name, param.default), param.annotation)
            for param in inspect.signature(func).parameters.values())), level=1, escape=False)
        func(**kwargs)
      (dst_log/'log.html').rename(dst_log/'index.html')

    refnode = docutils.nodes.reference('', '', internal=False, refuri=app.builder.get_relative_uri(env.docname, target))
    refnode.append(contnode)
    return refnode

def generate_api(app):
  nutils = project_root/'nutils'
  dst_root = pathlib.Path(app.srcdir)/'nutils'
  dst_root.mkdir(parents=True, exist_ok=True)

  srcs = tuple(f for f in sorted(nutils.glob('**/*.py')) if f != nutils/'__init__.py')
  for src in sphinx.util.status_iterator(srcs, 'generating api... ', 'purple', len(srcs), app.verbosity):
    module = '.'.join((src.parent if src.name == '__init__.py' else src.with_suffix('')).relative_to(nutils).parts)
    dst = dst_root/(module+'.rst')
    with dst.open('w') as f:
      print_rst_autogen_header(file=f, src=src)
      print_rst_h1(module, file=f)
      print('.. automodule:: {}'.format('nutils.{}'.format(module)), file=f)
    copy_utime(src, dst)

def remove_generated(app, exception):
  logger = sphinx.util.logging.getLogger(__name__)
  for name in 'nutils', 'examples':
    generated = pathlib.Path(app.srcdir)/name
    shutil.rmtree(str(generated), onerror=lambda f, p, e: logger.warning('failed to remove {}'.format(p)))

# Selected math symbols from https://en.wikipedia.org/wiki/Wikipedia:LaTeX_symbols
unicode_math_map = {
  # Letters
  'α':r'\alpha', 'β':r'\beta', 'γ':r'\gamma', 'δ':r'\delta', 'ϵ':r'\epsilon',
  'ζ':r'\zeta', 'η':r'\eta', 'θ':r'\theta', 'ι':r'\iota', 'κ':r'\kappa',
  'λ':r'\lambda', 'μ':r'\mu', 'ν':r'\nu', 'ξ':r'\xi', 'ο':r'\omicron',
  'π':r'\pi', 'ρ':r'\rho', 'σ':r'\sigma', 'τ':r'\tau', 'υ':r'\upsilon',
  'ϕ':r'\phi', 'χ':r'\chi', 'ψ':r'\psi', 'ω':r'\omega', 'ε':r'\varepsilon',
  'ϑ':r'\vartheta', 'ϰ':r'\varkappa', 'ϖ':r'\varpi', 'ϱ':r'\varrho',
  'φ':r'\varphi', 'ς':r'\varsigma', 'Γ':r'\Gamma', 'Δ':r'\Delta',
  'Θ':r'\Theta', 'Λ':r'\Lambda', 'Υ':r'\Upsilon', 'Ξ':r'\Xi', 'Φ':r'\Phi',
  'Π':r'\Pi', 'Ψ':r'\Psi', 'Σ':r'\Sigma', 'Ω':r'\Omega', 'ϝ':r'\digamma',
  'ℵ':r'\aleph', 'ℶ':r'\beth', 'ℷ':r'\gimel', 'ℸ':r'\daleth', '∀':r'\forall',
  '∃':r'\exists', '∄':r'\nexists', 'Ⅎ':r'\Finv', '⅁':r'\Game',
  '∍':r'\backepsilon', 'ı':r'\imath', 'ȷ':r'\jmath', 'ℓ':r'\ell',
  '⨿':r'\amalg', '∇':r'\nabla', '℧':r'\mho', '∂':r'\partial', 'ð':r'\eth',
  'ℏ':r'\hbar', 'ℏ':r'\hslash', 'ℑ':r'\Im', 'ℜ':r'\Re', '℘':r'\wp',
  '∅':r'\emptyset',
  # Big symbols
  '∫':r'\int', '∬':r'\iint', '∮':r'\oint', '∑':r'\sum',
  '∏':r'\prod', '⋂':r'\bigcap', '⋀':r'\bigwedge', '∐':r'\coprod',
  '⋃':r'\bigcup', '⋁':r'\bigvee', '⨆':r'\bigsqcup', '⨄':r'\biguplus',
  '⨁':r'\bigoplus', '⨂':r'\bigotimes', '⨀':r'\bigodot',
  # Arrows
  '←':r'\leftarrow', '⇐':r'\Leftarrow', '↼':r'\leftharpoonup',
  '↽':r'\leftharpoondown', '⇇':r'\leftleftarrows', '→':r'\rightarrow',
  '⇒':r'\Rightarrow', '⇀':r'\rightharpoonup', '⇁':r'\rightharpoondown',
  '⇉':r'\rightrightarrows', '↑':r'\uparrow', '⇑':r'\Uparrow',
  '↿':r'\upharpoonleft', '↾':r'\upharpoonright', '⇈':r'\upuparrows',
  '↓':r'\downarrow', '⇓':r'\Downarrow', '⇃':r'\downharpoonleft',
  '⇂':r'\downharpoonright', '⇊':r'\downdownarrows', '⟵':r'\longleftarrow',
  '⟸':r'\Longleftarrow', '↩':r'\hookleftarrow', '⇋':r'\leftrightharpoons',
  '⇆':r'\leftrightarrows', '⇚':r'\Lleftarrow', '↰':r'\Lsh',
  '↢':r'\leftarrowtail', '↞':r'\twoheadleftarrow', '↶':r'\curvearrowleft',
  '↺':r'\circlearrowleft', '↫':r'\looparrowleft', '⟶':r'\longrightarrow',
  '⟹':r'\Longrightarrow', '↪':r'\hookrightarrow', '⇌':r'\rightleftharpoons',
  '⇄':r'\rightleftarrows', '⇛':r'\Rrightarrow', '↱':r'\Rsh',
  '↣':r'\rightarrowtail', '↠':r'\twoheadrightarrow', '↷':r'\curvearrowright',
  '↻':r'\circlearrowright', '↬':r'\looparrowright', '↔':r'\leftrightarrow',
  '⟷':r'\longleftrightarrow', '↕':r'\updownarrow', '↚':r'\nleftarrow',
  '↛':r'\nrightarrow', '↮':r'\nleftrightarrow', '⇔':r'\Leftrightarrow',
  '⟺':r'\Longleftrightarrow', '⇕':r'\Updownarrow', '⇍':r'\nLeftarrow',
  '⇏':r'\nRightarrow', '⇎':r'\nLeftrightarrow', '↦':r'\mapsto',
  '⟼':r'\longmapsto', '⊸':r'\multimap', '⇝':r'\rightsquigarrow',
  '↭':r'\leftrightsquigarrow', '↙':r'\swarrow', '↘':r'\searrow',
  '↖':r'\nwarrow', '↗':r'\nearrow',
  # Order symbols
  '≤':r'\leq', '≦':r'\leqq', '≥':r'\geq', '≧':r'\geqq', '≮':r'\nless',
  '≰':r'\nleq', '≰':r'\nleqq', '≨':r'\lneqq', '≨':r'\lvertneqq', '≯':r'\ngtr',
  '≱':r'\ngeq', '≱':r'\ngeqq', '≩':r'\gneqq', '≩':r'\gvertneqq',
  '⊲':r'\vartriangleleft', '⊴':r'\trianglelefteq', '≲':r'\lesssim',
  '≺':r'\prec', '≾':r'\precsim', '⊳':r'\vartriangleright',
  '⊵':r'\trianglerighteq', '≳':r'\gtrsim', '≻':r'\succ', '≿':r'\succsim',
  '⋪':r'\ntriangleleft', '⋬':r'\ntrianglelefteq', '⋦':r'\lnsim', '⊀':r'\nprec',
  '⋠':r'\npreceq', '⋨':r'\precnsim', '⋫':r'\ntriangleright',
  '⋭':r'\ntrianglerighteq', '⋧':r'\gnsim', '⊁':r'\nsucc', '⋡':r'\nsucceq',
  '⋩':r'\succnsim', '≶':r'\lessgtr', '⋚':r'\lesseqgtr', '≷':r'\gtrless',
  '⋛':r'\gtreqless', '≪':r'\ll', '⋘':r'\lll', '⋖':r'\lessdot',
  '≼':r'\preccurlyeq', '⋞':r'\curlyeqprec', '≫':r'\gg', '⋙':r'\ggg',
  '⋗':r'\gtrdot', '≽':r'\succcurlyeq', '⋟':r'\curlyeqsucc',
  # Set symbols
  '⊂':r'\subset', '⋐':r'\Subset', '⊏':r'\sqsubset', '◃':r'\triangleleft',
  '◂':r'\blacktriangleleft', '⊃':r'\supset', '⋑':r'\Supset', '⊐':r'\sqsupset',
  '▹':r'\triangleright', '▸':r'\blacktriangleright', '∩':r'\cap', '⋒':r'\Cap',
  '⊓':r'\sqcap', '△':r'\vartriangle', '▴':r'\blacktriangle', '∪':r'\cup',
  '⋓':r'\Cup', '⊔':r'\sqcup', '▽':r'\triangledown', '▾':r'\blacktriangledown',
  '∈':r'\in', '⊆':r'\subseteq', '⊑':r'\sqsubseteq', '∋':r'\ni',
  '⊇':r'\supseteq', '⊒':r'\sqsupseteq', '∉':r'\notin', '⊈':r'\nsubseteq',
  '⊊':r'\subsetneq', '⊊':r'\varsubsetneq', '⊈':r'\nsubseteqq', '⊎':r'\uplus',
  '⊉':r'\nsupseteq', '⊋':r'\supsetneq', '⊋':r'\varsupsetneq',
  '⊉':r'\nsupseteqq',
  # Equality and inference
  '≠':r'\neq', '≡':r'\equiv', '≈':r'\thickapprox', '≈':r'\approx',
  '≊':r'\approxeq', '≅':r'\cong', '≆':r'\ncong', '∼':r'\sim', '∼':r'\thicksim',
  '≁':r'\nsim', '≃':r'\simeq', '∽':r'\backsim', '⋍':r'\backsimeq',
  '≂':r'\eqsim', '≐':r'\doteq', '÷':r'\div', '≑':r'\doteqdot',
  '≒':r'\fallingdotseq', '≓':r'\risingdotseq', '≜':r'\triangleq',
  '≗':r'\circeq', '≖':r'\eqcirc', '≏':r'\bumpeq', '≎':r'\Bumpeq',
  '≍':r'\asymp', '∣':r'\mid', '∣':r'\shortmid', '⊢':r'\vdash', '⊣':r'\dashv',
  '⊩':r'\Vdash', '∥':r'\parallel', '∥':r'\shortparallel', '⊨':r'\vDash',
  '⊪':r'\Vvdash', '⊨':r'\models', '∤':r'\nmid', '∤':r'\nshortmid',
  '⊬':r'\nvdash', '⊮':r'\nVdash', '∦':r'\nparallel', '∦':r'\nshortparallel',
  '⊭':r'\nvDash', '⊯':r'\nVDash',
  # Other symbols
  '∞':r'\infty', '∝':r'\propto', '∝':r'\varpropto', '⋈':r'\bowtie',
  '⋉':r'\ltimes', '⋊':r'\rtimes', '⊺':r'\intercal', '∔':r'\dotplus',
  '×':r'\times', '≀':r'\wr', '⋔':r'\pitchfork', '√':r'\surd', '∖':r'\setminus',
  '╲':r'\diagdown', '╱':r'\diagup', '⋋':r'\leftthreetimes',
  '⋌':r'\rightthreetimes', '⊥':r'\perp', '≬':r'\between', '≍':r'\asymp',
  '∠':r'\angle', '∡':r'\measuredangle', '∢':r'\sphericalangle', '⊥':r'\bot',
  '∧':r'\wedge', '⊼':r'\barwedge', '∴':r'\therefore', '⊤':r'\top', '∨':r'\vee',
  '⊻':r'\veebar', '∵':r'\because', '±':r'\pm', '⋏':r'\curlywedge',
  '⌢':r'\smallfrown', '⌢':r'\frown', '△':r'\bigtriangleup', '△':r'\triangle',
  '∓':r'\mp', '⋎':r'\curlyvee', '⌣':r'\smallsmile', '⌣':r'\smile',
  '▽':r'\bigtriangledown', '∘':r'\circ', '∙':r'\bullet', '⋅':r'\centerdot',
  '⋯':r'\cdots', '⋮':r'\vdots', '…':r'\ldots', '⋱':r'\ddots',
  '⊚':r'\circledcirc', '⊝':r'\circleddash', '⊛':r'\circledast',
  '◯':r'\bigcirc', '⋇':r'\divideontimes', '⊙':r'\odot', '⊕':r'\oplus',
  '⊖':r'\ominus', '⊗':r'\otimes', '⊘':r'\oslash', '⊡':r'\boxdot',
  '⊞':r'\boxplus', '⊟':r'\boxminus', '⊠':r'\boxtimes', '◻':r'\Box',
  '♢':r'\diamondsuit', '♣':r'\clubsuit', '♡':r'\heartsuit', '♠':r'\spadesuit',
  '◊':r'\lozenge', '⧫':r'\blacklozenge', '◻':r'\square', '◼':r'\blacksquare',
  '⋄':r'\diamond', '◊':r'\Diamond', '∗':r'\ast', '⋆':r'\star', '★':r'\bigstar',
  '♯':r'\sharp', '♭':r'\flat', '♮':r'\natural', '†':r'\dagger',
  '‡':r'\ddagger',
}
unicode_math_map = str.maketrans({k: v+' ' for k, v in unicode_math_map.items()})

def replace_unicode_math(app, doctree):
  if sphinx.version_info >= (1,8):
    math = sphinx.addnodes.math
  else:
    from sphinx.ext.mathbase import math
  for node in doctree.traverse(math):
    node['latex'] = node['latex'].translate(unicode_math_map)


class RequiresNode(docutils.nodes.Admonition, docutils.nodes.TextElement): pass

def html_visit_requires(self, node):
  self.body.append(self.starttag(node, 'div', CLASS='requires'))
def html_depart_requires(self, node):
  self.body.append('</div>\n')
def text_visit_requires(self, node):
  self.new_state(0)
def text_depart_requires(self, node):
  self.end_state()
def latex_visit_requires(self, node):
  pass
def latex_depart_requires(self, node):
  pass

class RequiresDirective(docutils.parsers.rst.Directive):

  has_content = False
  required_arguments = 1
  optional_arguments = 0

  def run(self):
    requires = tuple(name.strip() for name in self.arguments[0].split(','))

    node = RequiresNode('requires')
    node.document = self.state.document
    sphinx.util.nodes.set_source_info(self, node)
    msg = 'Requires {}.'.format(', '.join(requires))
    node.append(docutils.nodes.paragraph('', docutils.nodes.Text(msg, msg), translatable=False))
    return [node]


class ConsoleDirective(docutils.parsers.rst.Directive):

  has_content = True
  required_arguments = 0
  options_arguments = 0

  _console_log = treelog.FilterLog(treelog.StdoutLog(), minlevel=1)

  def run(self):
    document = self.state.document
    env = document.settings.env
    nodes = []

    indent = min(len(line)-len(line.lstrip()) for line in self.content)
    code = ''.join(line[indent:]+'\n' for line in self.content)
    code_wo_spread = nutils.testing.FloatNeighborhoodOutputChecker.re_spread.sub(lambda m: m.group(0).split('±', 1)[0], code)

    literal = docutils.nodes.literal_block(code_wo_spread, code_wo_spread, classes=['console'])
    literal['language'] = 'python3'
    literal['linenos'] = False
    sphinx.util.nodes.set_source_info(self, literal)
    nodes.append(literal)

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner(checker=nutils.testing.FloatNeighborhoodOutputChecker(), optionflags=doctest.ELLIPSIS)
    globs = getattr(document, '_console_globs', {})
    test = parser.get_doctest(code, globs, 'test', env.docname, self.lineno)
    with treelog.set(self._console_log):
      failures, tries = runner.run(test, clear_globs=False)
    for fignum in matplotlib.pyplot.get_fignums():
      fig = matplotlib.pyplot.figure(fignum)
      with io.BytesIO() as f:
        fig.savefig(f, format='svg')
        name = hashlib.sha1(f.getvalue()).hexdigest()+'.svg'
        uri = 'data:image/svg+xml;base64,{}'.format(base64.b64encode(f.getvalue()).decode())
        nodes.append(docutils.nodes.image('', uri=uri, alt='image generated by matplotlib'))
    matplotlib.pyplot.close('all')
    if failures:
      document.reporter.warning('doctest failed', line=self.lineno)
    document._console_globs = test.globs

    return nodes

def remove_console_globs(app, doctree):
  if hasattr(doctree, '_console_globs'):
    del doctree._console_globs

def setup(app):
  app.connect('autodoc-process-signature', process_signature)

  app.connect('builder-inited', generate_api)

  app.connect('builder-inited', generate_examples)
  app.add_directive('exampledoc', ExampleDocDirective)
  app.add_role('sh', role_sh)
  app.connect('missing-reference', create_log)

  app.connect('doctree-read', replace_unicode_math)

  app.add_node(RequiresNode,
               html=(html_visit_requires, html_depart_requires),
               latex=(latex_visit_requires, latex_depart_requires),
               text=(text_visit_requires, text_depart_requires))
  app.add_directive('requires', RequiresDirective)

  app.add_directive('console', ConsoleDirective)
  app.connect('doctree-read', remove_console_globs)

  app.connect('build-finished', remove_generated)

  if sphinx.version_info >= (1,8):
    app.add_css_file('mods.css')
  else:
    app.add_stylesheet('mods.css')

# vim: sts=2:sw=2:et
