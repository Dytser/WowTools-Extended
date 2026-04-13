import bpy
import os
import struct

from .wow_common import *
from .bone_mapper_bridge import apply_bone_map_pre_export, restore_bone_map_post_export

# sorts a list of vertex influences from heaviest to lightest
def VertexInfluenceSortKey(A):
	return -A[1]


def _get_export_mesh(context, BMesh, apply_modifiers, apply_shapekeys):
	"""
	Return a (mesh_data, weight_source_obj, needs_cleanup) triple.

	mesh_data        — the Blender mesh to read geometry/UVs from
	weight_source_obj — the object whose .vertex_groups and .data.vertices[i].groups
	                   should be used to read bone weights. This is always an object
	                   whose vertex index space matches mesh_data, so callers can do:
	                       weight_source_obj.data.vertices[VertexIndex].groups
	                   safely. When modifiers are applied the evaluated object is
	                   returned here — Blender propagates vertex groups through
	                   modifiers like Mirror automatically, so the mirrored half has
	                   correct weights without any manual lookup.
	needs_cleanup    — True when mesh_data is a temporary mesh that must be removed
	                   after export.

	The armature modifier is never baked. The original object is never modified.
	"""
	if not apply_modifiers and not apply_shapekeys:
		return BMesh.data, BMesh, False

	# Temporarily disable the armature modifier so it isn't baked into the
	# evaluated mesh. We restore it in the finally block regardless of outcome.
	armature_mods = [m for m in BMesh.modifiers if m.type == 'ARMATURE']
	armature_mod_states = {m.name: m.show_viewport for m in armature_mods}
	for m in armature_mods:
		m.show_viewport = False

	try:
		if apply_shapekeys and BMesh.data.shape_keys:
			import bmesh as _bmesh

			# Build a bmesh from the original mesh and blend active keys into it
			bm = _bmesh.new()
			bm.from_mesh(BMesh.data)

			key_blocks = BMesh.data.shape_keys.key_blocks
			basis = key_blocks[0].data  # index 0 is always the Basis
			for kb in key_blocks[1:]:
				if kb.value <= 0.0:
					continue  # discard inactive shape keys
				for vert, kb_point, basis_point in zip(bm.verts, kb.data, basis):
					delta = kb_point.co - basis_point.co
					vert.co += delta * kb.value

			# Write the mixed result into a temporary mesh
			tmp_mesh = bpy.data.meshes.new("__tmp_export__")
			bm.to_mesh(tmp_mesh)
			bm.free()

			# bmesh doesn't carry UV layers — copy them from the original
			for uv_layer in BMesh.data.uv_layers:
				tmp_layer = tmp_mesh.uv_layers.new(name=uv_layer.name)
				for i, datum in enumerate(uv_layer.data):
					tmp_layer.data[i].uv = datum.uv

			if apply_modifiers:
				# Link a temporary object carrying the mixed mesh, copy active
				# non-armature modifiers and vertex groups onto it, then evaluate
				# via depsgraph so modifiers (e.g. Mirror) propagate weights.
				tmp_obj = bpy.data.objects.new("__tmp_export_obj__", tmp_mesh)
				context.collection.objects.link(tmp_obj)

				# Copy vertex groups to the temporary object
				for vg in BMesh.vertex_groups:
					tmp_obj.vertex_groups.new(name=vg.name)
				for v in BMesh.data.vertices:
					for elem in v.groups:
						tmp_obj.vertex_groups[elem.group].add([v.index], elem.weight, 'REPLACE')

				for mod in BMesh.modifiers:
					if mod.type == 'ARMATURE' or not mod.show_viewport:
						continue
					new_mod = tmp_obj.modifiers.new(mod.name, mod.type)
					for prop in mod.bl_rna.properties:
						if prop.identifier in ('name', 'type', 'bl_rna'):
							continue
						try:
							setattr(new_mod, prop.identifier, getattr(mod, prop.identifier))
						except (AttributeError, TypeError):
							pass

				dg = context.evaluated_depsgraph_get()
				eval_obj = tmp_obj.evaluated_get(dg)
				# new_from_object bakes the geometry; eval_obj retains vertex groups
				final_mesh = bpy.data.meshes.new_from_object(eval_obj)

				# We must keep tmp_obj alive until after the caller reads weights
				# from eval_obj. Return eval_obj as weight_source so the caller
				# uses the correct (post-modifier) vertex index space.
				# Cleanup of tmp_obj/tmp_mesh is deferred to the caller via a
				# closure stored in needs_cleanup (we repurpose it as a callable).
				def _cleanup(obj=tmp_obj, mesh=tmp_mesh, col=context.collection):
					col.objects.unlink(obj)
					bpy.data.objects.remove(obj)
					bpy.data.meshes.remove(mesh)

				return final_mesh, eval_obj, _cleanup
			else:
				# Shape keys only — no modifier evaluation.
				# tmp_mesh vertex indices match the original, so use BMesh for weights.
				return tmp_mesh, BMesh, True

		else:
			# apply_modifiers only — evaluate the original object through the
			# depsgraph. Blender propagates vertex groups through modifiers
			# (including Mirror), so eval_obj has correct weights for all verts.
			dg = context.evaluated_depsgraph_get()
			eval_obj = BMesh.evaluated_get(dg)
			final_mesh = bpy.data.meshes.new_from_object(eval_obj)
			# eval_obj is valid as long as the depsgraph isn't invalidated.
			return final_mesh, eval_obj, True

	finally:
		# Always restore armature modifier viewport visibility
		for m in armature_mods:
			m.show_viewport = armature_mod_states[m.name]


def DoExport(FileName, use_selection=False, use_visible=False, use_active_collection=False,
		apply_modifiers=False, apply_shapekeys=False):
	"""Main export entry point."""
	MeshList = []
	BoneList = []
	AttachmentList = []
	CameraList = []

	if bpy.context.mode != 'OBJECT':
			print('Switching to OBJECT Mode...')
			bpy.ops.object.mode_set(mode='OBJECT')

	# Find armature
	BArmature = None
	for ob in bpy.context.scene.objects:
		if ob.type == 'ARMATURE' and ob.name == 'Armature':
			BArmature = ob
			break

	if BArmature is None:
		print('[WoW Tools] No armature named "Armature" found — export cancelled.')
		return

	# -----------------------------------------------------------------------
	# Phase order matters when modifiers are being applied:
	#   1. Bake all mesh geometry FIRST, while bones still have mirrored names
	#      (e.g. Arm_L / Arm_R) so the Mirror modifier can correctly mirror
	#      vertex weights to the opposite side.
	#   2. THEN rename bones to indexed names for writing.
	#   3. Write the file.
	#   4. Restore mirrored names.
	#
	# Bone rename + restore is handled inside _do_export_inner so it wraps
	# only the file-write phase, not the mesh-bake phase.
	# -----------------------------------------------------------------------
	_do_export_inner(FileName, BArmature, MeshList, BoneList, AttachmentList, CameraList,
		use_selection, use_visible, use_active_collection,
		apply_modifiers, apply_shapekeys)


def _do_export_inner(FileName, BArmature, MeshList, BoneList, AttachmentList, CameraList,
		use_selection=False, use_visible=False, use_active_collection=False,
		apply_modifiers=False, apply_shapekeys=False):
	"""Core export logic, extracted so the bone-map restore runs in a finally block."""

	context = bpy.context

	# -----------------------------------------------------------------------
	# Limit To: build the candidate set from armature children, then filter
	# -----------------------------------------------------------------------
	candidates = list(BArmature.children)

	if use_active_collection:
		active_col_objects = set(context.view_layer.active_layer_collection.collection.all_objects)
		candidates = [ob for ob in candidates if ob in active_col_objects]

	if use_selection:
		candidates = [ob for ob in candidates if ob.select_get()]

	if use_visible:
		candidates = [ob for ob in candidates if ob.visible_get()]

	# gather Blender objects
	BMeshList = []
	BEmptyList = []
	BCameraList = []
	for BObject in candidates:
		if BObject.type == 'MESH' and BObject.name.startswith('Mesh'):
			BMeshList.append(BObject)
		elif BObject.type == 'EMPTY' and BObject.name.startswith('Attach'):
			BEmptyList.append(BObject)
		elif BObject.type == 'CAMERA' and BObject.name.startswith('Camera'):
			BCameraList.append(BObject)

	MeshIndexesByName = dict()
	for i, BMesh in enumerate(BMeshList):
		MeshIndexesByName[BMesh.name] = i

	# Build a lookup: mirrored bone name (e.g. "Arm_L") → bone index (e.g. 51).
	# This is needed because when Apply Modifiers is enabled, mesh baking runs
	# while bones still have their friendly mirrored names so the Mirror modifier
	# works correctly. The evaluated vertex groups therefore carry mirrored names,
	# not "BoneNNN" names, so we need this table to resolve the index at write time.
	#
	# Strategy: scan ALL bone map .py files from the Bone-Mapper addon and accumulate
	# every (BoneNNN, MirroredName) pair. All maps share the same indexed skeleton so
	# there are no conflicts, and this avoids any fragile ID-string matching.
	_mirrored_to_index = {}
	try:
		import importlib.util as _ilu
		from pathlib import Path as _Path
		import addon_utils as _au
		_bm_dir = None
		for _m in _au.modules():
			if getattr(_m, "bl_info", {}).get("name") == "Bone-Mapper":
				_bm_dir = _Path(_m.__file__).parent / "bone_maps"
				break
		if _bm_dir and _bm_dir.is_dir():
			for _py in sorted(_bm_dir.rglob("*.py")):
				if _py.stem == "__init__":
					continue
				try:
					_spec = _ilu.spec_from_file_location("_bm_tmp." + _py.stem, _py)
					_bm_mod = _ilu.module_from_spec(_spec)
					_spec.loader.exec_module(_bm_mod)
					for _indexed, _mirrored in getattr(_bm_mod, "BONES", []):
						if _indexed.startswith("Bone"):
							try:
								_mirrored_to_index[_mirrored] = int(_indexed[4:].split('.')[0])
							except (ValueError, IndexError):
								pass
				except Exception:
					pass
	except Exception:
		pass

	def _resolve_bone_index(vgroup_name):
		"""Resolve a vertex group name to a bone index.
		Handles both BoneNNN (indexed) and mirrored names (Arm_L etc.)."""
		# Already an indexed name: Bone051, Bone051.001, etc.
		if vgroup_name.startswith('Bone'):
			try:
				return int(vgroup_name[4:].split('.')[0].split('_')[0])
			except (ValueError, IndexError):
				pass
		# Mirrored name — look up in the table built from the active bone map
		if vgroup_name in _mirrored_to_index:
			return _mirrored_to_index[vgroup_name]
		# Last resort: strip any suffix and try parsing digits after 'Bone'
		return None

	# extract meshes
	for BMesh in BMeshList:
		Mesh = CMesh()
		name = BMesh.name.split('.')[0]
		parts = name.split('_')
		id = int(parts[0][4:])
		props = BMesh.data.wow_props

		if len(props.MaterialOverride) > 0:
			if props.MaterialOverride not in MeshIndexesByName:
				raise Exception('Mesh \'' + BMesh.name + '\' has MaterialOverride property \'' + props.MaterialOverride + '\' that doesn\'t correspond to any mesh')
			Mesh.MaterialOverride = MeshIndexesByName[props.MaterialOverride]
		else:
			Mesh.MaterialOverride = -1

		Mesh.TextureTypes[0] = int(props.TextureType0)
		Mesh.TextureNames[0] = props.TextureName0
		Mesh.BlendMode = int(props.BlendMode)

		if props.MenuType == '0':
			Mesh.RenderFlags = RenderFlagsFromSet({'3'})  # TwoSided
			if len(props.TextureName1) > 0:
				Mesh.ShaderId = 32769
				Mesh.TextureTypes[1] = 0  # Hardcoded
				Mesh.TextureNames[1] = props.TextureName1
		elif props.MenuType == '1':
			Mesh.ShaderId = int(props.ShaderId)
			Mesh.RenderFlags = RenderFlagsFromSet(props.RenderFlags)
			Mesh.TextureTypes[1] = int(props.TextureType1)
			Mesh.TextureTypes[2] = int(props.TextureType2)
			Mesh.TextureTypes[3] = int(props.TextureType3)
			Mesh.TextureNames[1] = props.TextureName1
			Mesh.TextureNames[2] = props.TextureName2
			Mesh.TextureNames[3] = props.TextureName3

		Mesh.OriginalMeshIndex = props.OriginalMeshIndex
		Mesh.Description = props.Description
		Mesh.ID = id

		# ------------------------------------------------------------------
		# Get the mesh data to read from — may be an evaluated/temporary copy.
		# weight_obj is the object whose vertex_groups + data.vertices[i].groups
		# match the index space of mesh_data (including modifier-generated verts).
		# ------------------------------------------------------------------
		mesh_data, weight_obj, needs_cleanup = _get_export_mesh(context, BMesh, apply_modifiers, apply_shapekeys)

		try:
			for iFace, BFace in enumerate(mesh_data.polygons):
				FaceUVs = []
				FaceUVs2 = []

				for i in BFace.loop_indices:
					l = mesh_data.loops[i]

					tFound = False
					t2Found = False
					for j, ul in enumerate(mesh_data.uv_layers):
						if ul.name == 'Texture' or ul.name == 'UVMap':
							tFound = True
							FaceUVs.append([ul.data[l.index].uv[0], ul.data[l.index].uv[1]])
						elif ul.name == 'Texture2':
							t2Found = True
							FaceUVs2.append([ul.data[l.index].uv[0], ul.data[l.index].uv[1]])

					if not tFound:
						FaceUVs.append([0, 0])
					if not t2Found:
						FaceUVs2.append([0, 0])

				# build vertex list for this face
				FaceVertexList = []
				for iFaceVertex, VertexIndex in enumerate(BFace.vertices):
					BVertex = mesh_data.vertices[VertexIndex]
					Vertex = CMesh.CVertex()
					# position
					Vertex.Position[0] = BVertex.co.y
					Vertex.Position[1] = -BVertex.co.x
					Vertex.Position[2] = BVertex.co.z
					# normal
					if BFace.use_smooth:
						Vertex.Normal[0] = BVertex.normal.y
						Vertex.Normal[1] = -BVertex.normal.x
						Vertex.Normal[2] = BVertex.normal.z
					else:
						Vertex.Normal[0] = BFace.normal.y
						Vertex.Normal[1] = -BFace.normal.x
						Vertex.Normal[2] = BFace.normal.z
					# texture
					Vertex.Texture[0] = FaceUVs[iFaceVertex][0]
					Vertex.Texture[1] = 1.0 - FaceUVs[iFaceVertex][1]
					Vertex.Texture2[0] = FaceUVs2[iFaceVertex][0]
					Vertex.Texture2[1] = 1.0 - FaceUVs2[iFaceVertex][1]

					# Read bone weights from weight_obj, whose vertex index space
					# matches mesh_data exactly. For modifier-applied meshes this is
					# the evaluated object, so Mirror-generated verts have weights too.
					VertexInfluences = []
					w_vert = weight_obj.data.vertices[VertexIndex]
					for elem in w_vert.groups:
						if elem.weight > 0.0:
							VertexInfluences.append([weight_obj.vertex_groups[elem.group].name, elem.weight])
					VertexInfluences.sort(key=VertexInfluenceSortKey)
					VertexInfluences = VertexInfluences[:4]
					WeightSum = 0.0
					for VertexInfluence in VertexInfluences:
						WeightSum += VertexInfluence[1]
					if WeightSum > 0.0:
						for iBone, VertexInfluence in enumerate(VertexInfluences):
							Vertex.BoneWeights[iBone] = int(VertexInfluence[1] / WeightSum * 255.0)
							bone_idx = _resolve_bone_index(VertexInfluence[0])
							if bone_idx is None:
								raise Exception(
									f"Cannot resolve bone index for vertex group '{VertexInfluence[0]}'. "
									"Make sure the Bone-Mapper add-on is active and a bone map is applied."
								)
							Vertex.BoneIndices[iBone] = bone_idx
					WeightSum = Vertex.BoneWeights[0] + Vertex.BoneWeights[1] + Vertex.BoneWeights[2] + Vertex.BoneWeights[3]
					if WeightSum > 0:
						while WeightSum < 255:
							for iBone in range(0, 4):
								if Vertex.BoneWeights[iBone] > 0 and Vertex.BoneWeights[iBone] < 255:
									Vertex.BoneWeights[iBone] += 1
									WeightSum += 1
									break
					FaceVertexList.append(Vertex)

				# add triangle to mesh
				if len(FaceVertexList) == 3:
					Mesh.AddTriangle(FaceVertexList[0], FaceVertexList[1], FaceVertexList[2])
				elif len(FaceVertexList) == 4:
					Mesh.AddTriangle(FaceVertexList[0], FaceVertexList[1], FaceVertexList[2])
					Mesh.AddTriangle(FaceVertexList[2], FaceVertexList[3], FaceVertexList[0])

		finally:
			# Clean up the temporary mesh if one was created.
			# needs_cleanup is either True (remove mesh_data), False (nothing to do),
			# or a callable (deferred multi-object cleanup from the shapekeys+modifiers path).
			if callable(needs_cleanup):
				needs_cleanup()
			elif needs_cleanup:
				bpy.data.meshes.remove(mesh_data)

		MeshList.append(Mesh)

	# -----------------------------------------------------------------------
	# All mesh baking is now complete. Bones still have their friendly mirrored
	# names (e.g. Arm_L / Arm_R) during the entire mesh phase above, which is
	# what allows the Mirror modifier to correctly distribute weights to each
	# side. NOW it is safe to rename them to indexed names for writing.
	# -----------------------------------------------------------------------
	reverted_bone_map = apply_bone_map_pre_export(BArmature)

	try:
		# extract bones
		BoneMap = {}
		bpy.ops.object.select_all(action='DESELECT')
		BArmature.select_set(True)
		bpy.context.view_layer.objects.active = BArmature
		isHidden = BArmature.hide_get()
		BArmature.hide_set(False)
		bpy.ops.object.mode_set(mode='EDIT', toggle=False)

		for BBone in BArmature.data.edit_bones:
			Bone = CBone()
			if not BBone.name.startswith('Bone'):
				raise Exception('Bone \'' + BBone.name + '\' is not named properly. Proper convention is \'Bone[index]\'.')
			if BBone.parent != None:
				if not BBone.parent.name.startswith('Bone'):
					raise Exception('Bone \'' + BBone.parent.name + '\' is not named properly. Proper convention is \'Bone[index]\'.')
			Bone.Index = int(BBone.name[4:].split('.')[0])
			if BBone.parent != None:
				Bone.Parent = int(BBone.parent.name[4:].split('.')[0])
			Bone.Position[0] = BBone.head.y
			Bone.Position[1] = -BBone.head.x
			Bone.Position[2] = BBone.head.z

			props = BBone.wow_props
			Bone.HasData = props.HasData
			Bone.Flags = BoneFlagsFromSet(props.Flags)
			Bone.SubmeshId = props.SubmeshId
			Bone.Unknown[0] = props.Unknown0
			Bone.Unknown[1] = props.Unknown1
			Bone.KeyBone = props.KeyBone

			BoneList.append(Bone)
			BoneMap[BBone.name] = BBone
		bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

		# extract attachments
		for BEmpty in BEmptyList:
			Attachment = CAttachment()
			Attachment.ID = int(BEmpty.name[6:].split('.')[0])
			Attachment.Parent = int(BEmpty.parent_bone[4:].split('.')[0])
			BBone = BArmature.data.bones[BEmpty.parent_bone]
			Attachment.Position[0] = BEmpty.location.y + BBone.head_local[1] + 0.1
			Attachment.Position[1] = -BEmpty.location.x - BBone.head_local[0]
			Attachment.Position[2] = BEmpty.location.z + BBone.head_local[2]
			Attachment.Scale = 1.0
			AttachmentList.append(Attachment)

		# extract cameras
		for BCamera in BCameraList:
			Camera = CCamera()
			props = BCamera.data.wow_props
			Camera.HasData = props.HasData
			Camera.Type = int(props.Type)
			Camera.Position[0] = BCamera.location.y
			Camera.Position[1] = -BCamera.location.x
			Camera.Position[2] = BCamera.location.z
			Camera.Target[0] = props.TargetY
			Camera.Target[1] = -props.TargetX
			Camera.Target[2] = props.TargetZ
			Camera.FieldOfView = BCamera.data.angle
			Camera.ClipNear = BCamera.data.clip_start
			Camera.ClipFar = BCamera.data.clip_end
			CameraList.append(Camera)

		BArmature.hide_set(isHidden)

		# open stream
		File = open(FileName, 'wb')
		DataBinary = CDataBinary(File, EEndianness.Little)

		# save header
		DataBinary.WriteUInt32(MakeFourCC(b'M2I0'))
		DataBinary.WriteUInt16(8)
		DataBinary.WriteUInt16(1)

		# save mesh list
		DataBinary.WriteUInt32(len(MeshList))
		for Mesh in MeshList:
			DataBinary.WriteUInt16(Mesh.ID)
			DataBinary.WriteNullterminatedString(Mesh.Description)
			DataBinary.WriteSInt16(Mesh.MaterialOverride)
			DataBinary.WriteSInt32(Mesh.ShaderId)
			DataBinary.WriteSInt16(Mesh.BlendMode)
			DataBinary.WriteUInt16(Mesh.RenderFlags)
			for i in range(0, 4):
				DataBinary.WriteSInt16(Mesh.TextureTypes[i])
				DataBinary.WriteNullterminatedString(Mesh.TextureNames[i])
			DataBinary.WriteSInt32(Mesh.OriginalMeshIndex)
			DataBinary.WriteUInt16(0)  # Level
			DataBinary.WriteUInt32(len(Mesh.VertexList))
			for Vertex in Mesh.VertexList:
				DataBinary.WriteFloat32(Vertex.Position[0])
				DataBinary.WriteFloat32(Vertex.Position[1])
				DataBinary.WriteFloat32(Vertex.Position[2])
				DataBinary.WriteUInt8(Vertex.BoneWeights[0])
				DataBinary.WriteUInt8(Vertex.BoneWeights[1])
				DataBinary.WriteUInt8(Vertex.BoneWeights[2])
				DataBinary.WriteUInt8(Vertex.BoneWeights[3])
				DataBinary.WriteUInt8(Vertex.BoneIndices[0])
				DataBinary.WriteUInt8(Vertex.BoneIndices[1])
				DataBinary.WriteUInt8(Vertex.BoneIndices[2])
				DataBinary.WriteUInt8(Vertex.BoneIndices[3])
				DataBinary.WriteFloat32(Vertex.Normal[0])
				DataBinary.WriteFloat32(Vertex.Normal[1])
				DataBinary.WriteFloat32(Vertex.Normal[2])
				DataBinary.WriteFloat32(Vertex.Texture[0])
				DataBinary.WriteFloat32(Vertex.Texture[1])
				DataBinary.WriteFloat32(Vertex.Texture2[0])
				DataBinary.WriteFloat32(Vertex.Texture2[1])
			DataBinary.WriteUInt32(len(Mesh.TriangleList))
			for Triangle in Mesh.TriangleList:
				DataBinary.WriteUInt16(Triangle.A)
				DataBinary.WriteUInt16(Triangle.B)
				DataBinary.WriteUInt16(Triangle.C)

		# save bone list
		DataBinary.WriteUInt32(len(BoneList))
		for Bone in BoneList:
			DataBinary.WriteUInt16(Bone.Index)
			DataBinary.WriteSInt16(Bone.Parent)
			DataBinary.WriteFloat32(Bone.Position[0])
			DataBinary.WriteFloat32(Bone.Position[1])
			DataBinary.WriteFloat32(Bone.Position[2])
			DataBinary.WriteUInt8(Bone.HasData)
			DataBinary.WriteUInt32(Bone.Flags)
			DataBinary.WriteUInt16(Bone.SubmeshId)
			DataBinary.WriteUInt16(Bone.Unknown[0])
			DataBinary.WriteUInt16(Bone.Unknown[1])

		# save attachment list
		DataBinary.WriteUInt32(len(AttachmentList))
		for Attachment in AttachmentList:
			DataBinary.WriteUInt32(Attachment.ID)
			DataBinary.WriteSInt16(Attachment.Parent)
			DataBinary.WriteFloat32(Attachment.Position[0])
			DataBinary.WriteFloat32(Attachment.Position[1])
			DataBinary.WriteFloat32(Attachment.Position[2])
			DataBinary.WriteFloat32(Attachment.Scale)

		# save camera list
		DataBinary.WriteUInt32(len(CameraList))
		for Camera in CameraList:
			DataBinary.WriteUInt8(Camera.HasData)
			DataBinary.WriteSInt32(Camera.Type)
			DataBinary.WriteFloat32(Camera.FieldOfView)
			DataBinary.WriteFloat32(Camera.ClipFar)
			DataBinary.WriteFloat32(Camera.ClipNear)
			DataBinary.WriteFloat32(Camera.Position[0])
			DataBinary.WriteFloat32(Camera.Position[1])
			DataBinary.WriteFloat32(Camera.Position[2])
			DataBinary.WriteFloat32(Camera.Target[0])
			DataBinary.WriteFloat32(Camera.Target[1])
			DataBinary.WriteFloat32(Camera.Target[2])

		# close stream
		File.close()

		print('M2I exported successfully: ' + FileName)

	finally:
		# Always restore mirrored names in the scene, even if export fails
		if reverted_bone_map is not None:
			restore_bone_map_post_export(BArmature, reverted_bone_map)


class M2IExporter(bpy.types.Operator):
	"""Export a M2 Intermediate file"""
	bl_idname = "export.m2i"
	bl_label = "Export M2I"

	filepath: bpy.props.StringProperty(name='File Path', description='Filepath used for exporting the M2I file', maxlen=1024, default='')
	check_existing: bpy.props.BoolProperty(name='Check Existing', description='Check and warn on overwriting existing files', default=True, options={'HIDDEN'})
	filter_glob: bpy.props.StringProperty(default='*.m2i', options={'HIDDEN'})

	# Limit To
	use_selection: bpy.props.BoolProperty(
		name='Selected Objects',
		description='Export only currently selected objects',
		default=False,
	)
	use_visible: bpy.props.BoolProperty(
		name='Visible Objects',
		description='Export only objects visible in the viewport',
		default=False,
	)
	use_active_collection: bpy.props.BoolProperty(
		name='Active Collection',
		description='Export only objects in the active collection',
		default=False,
	)

	# Mesh
	apply_modifiers: bpy.props.BoolProperty(
		name='Apply Modifiers',
		description=(
			'Apply active modifiers before export. '
			'Inactive modifiers are discarded. '
			'The Armature modifier is always excluded'
		),
		default=False,
	)
	apply_shapekeys: bpy.props.BoolProperty(
		name='Apply Shape Keys',
		description=(
			'Blend active shape keys (value > 0) into the exported mesh. '
			'Shape keys with a value of 0 are discarded. '
			'The original shape keys are never modified'
		),
		default=False,
	)

	def draw(self, context):
		layout = self.layout

		box = layout.box()
		box.label(text='Limit To:')
		box.prop(self, 'use_selection')
		box.prop(self, 'use_visible')
		box.prop(self, 'use_active_collection')

		box = layout.box()
		box.label(text='Mesh:')
		box.prop(self, 'apply_modifiers')
		box.prop(self, 'apply_shapekeys')

	def execute(self, context):
		FilePath = self.filepath
		if not FilePath.lower().endswith('.m2i'):
			FilePath += '.m2i'
		DoExport(
			FilePath,
			use_selection=self.use_selection,
			use_visible=self.use_visible,
			use_active_collection=self.use_active_collection,
			apply_modifiers=self.apply_modifiers,
			apply_shapekeys=self.apply_shapekeys,
		)
		return {'FINISHED'}

	def invoke(self, context, event):
		WindowManager = context.window_manager
		WindowManager.fileselect_add(self)
		return {'RUNNING_MODAL'}
