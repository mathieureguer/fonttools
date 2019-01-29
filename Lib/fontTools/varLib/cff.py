import os
from fontTools.cffLib import (
	maxStackLimit,
	TopDictIndex,
	buildOrder,
	topDictOperators,
	topDictOperators2,
	privateDictOperators,
	privateDictOperators2,
	FDArrayIndex,
	FontDict,
	VarStoreData
)
from fontTools.misc.py23 import BytesIO
from fontTools.cffLib.specializer import (
	specializeCommands, commandsToProgram)
from fontTools.ttLib import newTable
from fontTools import varLib
from fontTools.varLib.models import allEqual
from fontTools.misc.psCharStrings import T2CharString, T2OutlineExtractor
from fontTools.pens.t2CharStringPen import T2CharStringPen, t2c_round


def addCFFVarStore(varFont, varModel, var_data_list, master_supports):
	fvarTable = varFont['fvar']
	axisKeys = [axis.axisTag for axis in fvarTable.axes]
	varTupleList = varLib.builder.buildVarRegionList(master_supports, axisKeys)
	varStoreCFFV = varLib.builder.buildVarStore(varTupleList, var_data_list)

	topDict = varFont['CFF2'].cff.topDictIndex[0]
	topDict.VarStore = VarStoreData(otVarStore=varStoreCFFV)


def lib_convertCFFToCFF2(cff, otFont):
	# This assumes a decompiled CFF table.
	cff2GetGlyphOrder = cff.otFont.getGlyphOrder
	topDictData = TopDictIndex(None, cff2GetGlyphOrder, None)
	topDictData.items = cff.topDictIndex.items
	cff.topDictIndex = topDictData
	topDict = topDictData[0]
	if hasattr(topDict, 'Private'):
		privateDict = topDict.Private
	else:
		privateDict = None
	opOrder = buildOrder(topDictOperators2)
	topDict.order = opOrder
	topDict.cff2GetGlyphOrder = cff2GetGlyphOrder
	if not hasattr(topDict, "FDArray"):
		fdArray = topDict.FDArray = FDArrayIndex()
		fdArray.strings = None
		fdArray.GlobalSubrs = topDict.GlobalSubrs
		topDict.GlobalSubrs.fdArray = fdArray
		charStrings = topDict.CharStrings
		if charStrings.charStringsAreIndexed:
			charStrings.charStringsIndex.fdArray = fdArray
		else:
			charStrings.fdArray = fdArray
		fontDict = FontDict()
		fontDict.setCFF2(True)
		fdArray.append(fontDict)
		fontDict.Private = privateDict
		privateOpOrder = buildOrder(privateDictOperators2)
		for entry in privateDictOperators:
			key = entry[1]
			if key not in privateOpOrder:
				if key in privateDict.rawDict:
					# print "Removing private dict", key
					del privateDict.rawDict[key]
				if hasattr(privateDict, key):
					delattr(privateDict, key)
					# print "Removing privateDict attr", key
	else:
		# clean up the PrivateDicts in the fdArray
		fdArray = topDict.FDArray
		privateOpOrder = buildOrder(privateDictOperators2)
		for fontDict in fdArray:
			fontDict.setCFF2(True)
			for key in list(fontDict.rawDict.keys()):
				if key not in fontDict.order:
					del fontDict.rawDict[key]
					if hasattr(fontDict, key):
						delattr(fontDict, key)

			privateDict = fontDict.Private
			for entry in privateDictOperators:
				key = entry[1]
				if key not in privateOpOrder:
					if key in privateDict.rawDict:
						# print "Removing private dict", key
						del privateDict.rawDict[key]
					if hasattr(privateDict, key):
						delattr(privateDict, key)
						# print "Removing privateDict attr", key
	# Now delete up the decrecated topDict operators from CFF 1.0
	for entry in topDictOperators:
		key = entry[1]
		if key not in opOrder:
			if key in topDict.rawDict:
				del topDict.rawDict[key]
			if hasattr(topDict, key):
				delattr(topDict, key)

	# At this point, the Subrs and Charstrings are all still T2Charstring class
	# easiest to fix this by compiling, then decompiling again
	cff.major = 2
	file = BytesIO()
	cff.compile(file, otFont, isCFF2=True)
	file.seek(0)
	cff.decompile(file, otFont, isCFF2=True)


def convertCFFtoCFF2(varFont):
	# Convert base font to a single master CFF2 font.
	cffTable = varFont['CFF ']
	lib_convertCFFToCFF2(cffTable.cff, varFont)
	newCFF2 = newTable("CFF2")
	newCFF2.cff = cffTable.cff
	varFont['CFF2'] = newCFF2
	del varFont['CFF ']


class MergeDictError(TypeError):
	def __init__(self, key, value, values):
		error_msg = [
						"For the Private Dict key '{}', ".format(key),
						"the default font value list:",
						"\t{}".format(value),
						"had a different number of values than a region font:"]
		error_msg += ["\t{}".format(region_value) for region_value in values]
		error_msg = os.linesep.join(error_msg)


def conv_to_int(num):
	if num % 1 == 0:
		return int(num)
	return num


pd_blend_fields = (
					"BlueValues", "OtherBlues", "FamilyBlues",
					"FamilyOtherBlues", "BlueScale", "BlueShift",
					"BlueFuzz", "StdHW", "StdVW", "StemSnapH",
					"StemSnapV")


def get_private(regionFDArrays, fd_index, ri, fd_map):
	region_fdArray = regionFDArrays[ri]
	region_fd_map = fd_map[fd_index]
	if ri in region_fd_map:
		region_fdIndex = region_fd_map[ri]
		private = region_fdArray[region_fdIndex].Private
	else:
		private = None
	return private


def merge_PrivateDicts(top_dicts, vsindex_dict, var_model, fd_map):
	"""
	I step through the FontDicts in the FDArray of the varfont TopDict.
	For each varfont FontDict:
		step through each key in FontDict.Private.
		For each key, step through each relevant source font Private dict, and
		build a list of values to blend.
	The 'relevant' source fonts are selected by first getting the right
	submodel using model_keys[vsindex]. The indices of the
	subModel.locations are mapped to source font list indices by
	assuming the latter order is the same as the order of the
	var_model.locations. I can then get the index of each subModel
	location in the list of var_model.locations.
	"""

	topDict = top_dicts[0]
	region_top_dicts = top_dicts[1:]
	if hasattr(region_top_dicts[0], 'FDArray'):
		regionFDArrays = [fdTopDict.FDArray for fdTopDict in region_top_dicts]
	else:
		regionFDArrays = [[fdTopDict] for fdTopDict in region_top_dicts]
	for fd_index, font_dict in enumerate(topDict.FDArray):
		private_dict = font_dict.Private
		vsindex = getattr(private_dict, 'vsindex', 0)
		# At the moment, no PrivateDict has a vsindex key, but let's support
		# how it should work. See comment at end of
		# merge_charstrings() - still need to optimize use of vsindex.
		sub_model, model_keys = vsindex_dict[vsindex]
		master_indices = []
		for loc in sub_model.locations[1:]:
			i = var_model.locations.index(loc) - 1
			master_indices.append(i)
		pds = [private_dict]
		last_pd = private_dict
		for ri in master_indices:
			pd = get_private(regionFDArrays, fd_index, ri, fd_map)
			# If the region font doesn't have this FontDict, just reference
			# the last one used.
			if pd is None:
				pd = last_pd
			else:
				last_pd = pd
			pds.append(pd)
		num_masters = len(pds)
		for key, value in private_dict.rawDict.items():
			if key not in pd_blend_fields:
				continue
			if isinstance(value, list):
				try:
					values = [pd.rawDict[key] for pd in pds]
				except KeyError:
					del private_dict.rawDict[key]
					print(
						b"Warning: {key} in default font Private dict is "
						b"missing from another font, and was "
						b"discarded.".format(key=key))
					continue
				try:
					values = zip(*values)
				except IndexError:
					raise MergeDictError(key, value, values)
				"""
				Row 0 contains the first  value from each master.
				Convert each row from absolute values to relative
				values from the previous row.
				e.g for three masters,	a list of values was:
				master 0 OtherBlues = [-217,-205]
				master 1 OtherBlues = [-234,-222]
				master 1 OtherBlues = [-188,-176]
				The call to zip() converts this to:
				[(-217, -234, -188), (-205, -222, -176)]
				and is converted finally to:
				OtherBlues = [[-217, 17.0, 46.0], [-205, 0.0, 0.0]]
				"""
				dataList = []
				prev_val_list = [0] * num_masters
				any_points_differ = False
				for val_list in values:
					rel_list = [(val - prev_val_list[i]) for (
							i, val) in enumerate(val_list)]
					if (not any_points_differ) and not allEqual(rel_list):
						any_points_differ = True
					prev_val_list = val_list
					deltas = sub_model.getDeltas(rel_list)
					# Convert numbers with no decimal part to an int.
					deltas = [conv_to_int(delta) for delta in deltas]
					# For PrivateDict BlueValues, the default font
					# values are absolute, not relative to the prior value.
					deltas[0] = val_list[0]
					dataList.append(deltas)
				# If there are no blend values,then
				# we can collapse the blend lists.
				if not any_points_differ:
					dataList = [data[0] for data in dataList]
			else:
				values = [pd.rawDict[key] for pd in pds]
				if not allEqual(values):
					dataList = sub_model.getDeltas(values)
				else:
					dataList = values[0]
			private_dict.rawDict[key] = dataList


def getfd_map(varFont, fonts_list):
	""" Since a subset source font may have fewer FontDicts in their
	FDArray than the default font, we have to match up the FontDicts in
	the different fonts . We do this with the FDSelect array, and by
	assuming that the same glyph will reference  matching FontDicts in
	each source font. We return a mapping from fdIndex in the default
	font to a dictionary which maps each master list index of each
	region font to the equivalent fdIndex in the region font."""
	fd_map = {}
	default_font = fonts_list[0]
	region_fonts = fonts_list[1:]
	num_regions = len(region_fonts)
	topDict = default_font['CFF '].cff.topDictIndex[0]
	if not hasattr(topDict, 'FDSelect'):
		fd_map[0] = [0]*num_regions
		return fd_map

	gname_mapping = {}
	default_fdSelect = topDict.FDSelect
	glyphOrder = default_font.getGlyphOrder()
	for gid, fdIndex in enumerate(default_fdSelect):
		gname_mapping[glyphOrder[gid]] = fdIndex
		if fdIndex not in fd_map:
			fd_map[fdIndex] = {}
	for ri, region_font in enumerate(region_fonts):
		region_glyphOrder = region_font.getGlyphOrder()
		region_topDict = region_font['CFF '].cff.topDictIndex[0]
		if not hasattr(region_topDict, 'FDSelect'):
			# All the glyphs share the same FontDict. Pick any glyph.
			default_fdIndex = gname_mapping[region_glyphOrder[0]]
			fd_map[default_fdIndex][ri] = 0
		else:
			region_fdSelect = region_topDict.FDSelect
			for gid, fdIndex in enumerate(region_fdSelect):
				default_fdIndex = gname_mapping[region_glyphOrder[gid]]
				region_map = fd_map[default_fdIndex]
				if ri not in region_map:
					region_map[ri] = fdIndex
	return fd_map


def merge_region_fonts(varFont, model, ordered_fonts_list, glyphOrder):
	topDict = varFont['CFF2'].cff.topDictIndex[0]
	top_dicts = [topDict] + [
			ttFont['CFF '].cff.topDictIndex[0]
			for ttFont in ordered_fonts_list[1:]
				]
	num_masters = len(model.mapping)
	(var_data_list, master_supports,
		vsindex_dict) = merge_charstrings(
							glyphOrder, num_masters, top_dicts, model)
	fd_map = getfd_map(varFont, ordered_fonts_list)
	merge_PrivateDicts(top_dicts, vsindex_dict, model, fd_map)
	addCFFVarStore(varFont, model, var_data_list, master_supports)


def _get_cs(glyphOrder, charstrings, glyphName):
	if glyphName not in charstrings:
		return None
	return charstrings[glyphName]


def merge_charstrings(glyphOrder, num_masters, top_dicts, masterModel):

	vsindex_dict = {}
	var_data_list = []
	master_supports = []
	default_charstrings = top_dicts[0].CharStrings
	for gid, gname in enumerate(glyphOrder):
		all_cs = [
				_get_cs(glyphOrder, td.CharStrings, gname)
				for td in top_dicts]
		if len([gs for gs in all_cs if gs is not None]) == 1:
			continue
		model, model_cs = masterModel.getSubModel(all_cs)

		# create the first pass CFF2 charstring, from
		# the default charstring.
		default_charstring = model_cs[0]
		var_pen = CFF2CharStringMergePen([], gname, num_masters, 0)
		# We need to override outlineExtractor because these
		# charstrings do have widths in the 'program'; we need to drop these
		# values rather than post assertion error for them.
		default_charstring.outlineExtractor = CFFToCFF2OutlineExtractor
		default_charstring.draw(var_pen)

		# Add the coordinates from all the other regions to the
		# blend lists in the CFF2 charstring.
		region_cs = model_cs[1:]
		for region_idx, region_charstring in enumerate(region_cs, start=1):
			var_pen.restart(region_idx)
			region_charstring.draw(var_pen)

		# Collapse each coordinate list to a blend operator and its args.
		new_cs = var_pen.getCharString(
			private=default_charstring.private,
			globalSubrs=default_charstring.globalSubrs,
			var_model=model, optimize=True)
		default_charstrings[gname] = new_cs

		if (not var_pen.seen_moveto) or ('blend' not in new_cs.program):
			# If this is not a marking glyph, or if there are no blend
			# arguments, then we can use vsindex 0. No need to
			# check if we need a new vsindex.
			continue

		# If the charstring required a new model, create
		# a VarData table to go with, and set vsindex.
		try:
			key = tuple(v is not None for v in all_cs)
			vsindex = vsindex_dict[key]
			vsindex_dict[vsindex] = (model, key)
		except KeyError:
			varTupleIndexes = []
			for support in model.supports[1:]:
				if support not in master_supports:
					master_supports.append(support)
				varTupleIndexes.append(master_supports.index(support))
			var_data = varLib.builder.buildVarData(varTupleIndexes, None, False)
			vsindex = len(vsindex_dict)
			vsindex_dict[vsindex] = (model, key)
			var_data_list.append(var_data)
		# We do not need to check for an existing new_cs.private.vsindex,
		# as we know it doesn't exist yet.
		if vsindex != 0:
			new_cs.program[:0] = [vsindex, 'vsindex']

	# XXX To do: optimize use of vsindex between the PrivateDicts
	return var_data_list, master_supports, vsindex_dict


class MergeTypeError(TypeError):
	def __init__(self, point_type, pt_index, m_index, default_type, glyphName):
			self.error_msg = [
						"In glyph '{gname}' "
						"'{point_type}' at point index {pt_index} in master "
						"index {m_index} differs from the default font point "
						"type '{default_type}'"
						"".format(
								gname=glyphName,
								point_type=point_type, pt_index=pt_index,
								m_index=m_index, default_type=default_type)
						][0]
			super(MergeTypeError, self).__init__(self.error_msg)


def makeRoundNumberFunc(tolerance):
	if tolerance < 0:
		raise ValueError("Rounding tolerance must be positive")

	def roundNumber(val):
		return t2c_round(val, tolerance)

	return roundNumber


class CFFToCFF2OutlineExtractor(T2OutlineExtractor):
	""" This class is used to remove the initial width from the CFF
	charstring without trying to add the width to self.nominalWidthX,
	which is None. """
	def popallWidth(self, evenOdd=0):
		args = self.popall()
		if not self.gotWidth:
			if evenOdd ^ (len(args) % 2):
				args = args[1:]
			self.width = self.defaultWidthX
			self.gotWidth = 1
		return args


class CFF2CharStringMergePen(T2CharStringPen):
	"""Pen to merge Type 2 CharStrings.
	"""
	def __init__(
				self, default_commands, glyphName, num_masters, master_idx,
				roundTolerance=0.5, CFF2=True):
		super(
			CFF2CharStringMergePen,
			self).__init__(
							width=None,
							glyphSet=None, CFF2=CFF2,
							roundTolerance=roundTolerance)
		self.pt_index = 0
		self._commands = default_commands
		self.m_index = master_idx
		self.num_masters = num_masters
		self.prev_move_idx = 0
		self.seen_moveto = False
		self.glyphName = glyphName
		self.roundNumber = makeRoundNumberFunc(roundTolerance)

	def add_point(self, point_type, pt_coords):
		if self.m_index == 0:
			self._commands.append([point_type, [pt_coords]])
		else:
			cmd = self._commands[self.pt_index]
			if cmd[0] != point_type:
				raise MergeTypeError(
									point_type,
									self.pt_index, len(cmd[1]),
									cmd[0], self.glyphName)
			cmd[1].append(pt_coords)
		self.pt_index += 1

	def _p(self, pt):
		""" Unlike T2CharstringPen, this class stores absolute values.
		"""
		self._p0 = pt
		return list(self._p0)

	def _moveTo(self, pt):
		if not self.seen_moveto:
			self.seen_moveto = True
		pt_coords = self._p(pt)
		self.add_point('rmoveto', pt_coords)
		# I set prev_move_idx here because add_point()
		# can change self.pt_index.
		self.prev_move_idx = self.pt_index - 1

	def _lineTo(self, pt):
		pt_coords = self._p(pt)
		self.add_point('rlineto', pt_coords)

	def _curveToOne(self, pt1, pt2, pt3):
		_p = self._p
		pt_coords = _p(pt1)+_p(pt2)+_p(pt3)
		self.add_point('rrcurveto', pt_coords)

	def _closePath(self):
		pass

	def _endPath(self):
		pass

	def restart(self, region_idx):
		self.pt_index = 0
		self.m_index = region_idx
		self._p0 = (0, 0)

	def getCommands(self):
		return self._commands

	def reorder_blend_args(self, commands):
		"""
		We first re-order the master coordinate values.
		For a moveto to lineto, the args are now arranged as:
			[ [master_0 x,y], [master_1 x,y], [master_2 x,y] ]
		We re-arrange this to
		[	[master_0 x, master_1 x, master_2 x],
			[master_0 y, master_1 y, master_2 y]
		]
		We also make the value relative.
		If the master values are all the same, we collapse the list to
		as single value instead of a list.
		"""
		for cmd in commands:
			# arg[i] is the set of arguments for this operator from master i.
			args = cmd[1]
			m_args = zip(*args)
			# m_args[n] is now all num_master args for the i'th argument
			# for this operation.
			cmd[1] = m_args

		# Now convert from absolute to relative
		x0 = [0]*self.num_masters
		y0 = [0]*self.num_masters
		for cmd in self._commands:
			is_x = True
			coords = cmd[1]
			rel_coords = []
			for coord in coords:
				prev_coord = x0 if is_x else y0
				rel_coord = [pt[0] - pt[1] for pt in zip(coord, prev_coord)]

				if allEqual(rel_coord):
					rel_coord = rel_coord[0]
				rel_coords.append(rel_coord)
				if is_x:
					x0 = coord
				else:
					y0 = coord
				is_x = not is_x
			cmd[1] = rel_coords
		return commands

	def getCharString(
					self, private=None, globalSubrs=None,
					var_model=None, optimize=True):
		commands = self._commands
		commands = self.reorder_blend_args(commands)
		if optimize:
			commands = specializeCommands(
						commands, generalizeFirst=False,
						maxstack=maxStackLimit)
		program = commandsToProgram(
						commands, get_deltas=var_model.getDeltas,
						round_func=self.roundNumber)
		charString = T2CharString(
						program=program, private=private,
						globalSubrs=globalSubrs)
		return charString
